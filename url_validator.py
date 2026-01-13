"""
URL Validator Module - URL 安全验证模块

防止 SSRF（Server-Side Request Forgery）攻击，包括：
- 限制 scheme 只允许 http/https
- 拒绝访问私有网络地址（localhost, 内网 IP, 链路本地地址等）
- DNS 解析后验证 IP（防止域名解析到内网 IP）
- 支持域名白名单和黑名单
"""

import asyncio
import ipaddress
import socket
import re
import fnmatch
from typing import Tuple, List, Optional, Set
from urllib.parse import urlparse

from astrbot.api import logger


class URLValidationError(Exception):
    """URL 验证失败异常"""
    pass


class URLValidator:
    """URL 安全验证器
    
    用于验证 URL 是否安全，防止 SSRF 攻击。
    
    Attributes:
        allow_private_network: 是否允许访问私有网络
        allowed_domains: 允许的域名列表（支持通配符）
        blocked_domains: 拒绝的域名列表（支持通配符）
    """
    
    # 允许的 URL schemes
    ALLOWED_SCHEMES = {'http', 'https'}
    
    # 私有/保留 IP 范围
    # IPv4
    PRIVATE_IPV4_NETWORKS = [
        ipaddress.ip_network('127.0.0.0/8'),      # Loopback
        ipaddress.ip_network('10.0.0.0/8'),       # Private-Use
        ipaddress.ip_network('172.16.0.0/12'),    # Private-Use
        ipaddress.ip_network('192.168.0.0/16'),   # Private-Use
        ipaddress.ip_network('169.254.0.0/16'),   # Link-Local (包含 AWS 元数据 169.254.169.254)
        ipaddress.ip_network('0.0.0.0/8'),        # "This" Network
        ipaddress.ip_network('100.64.0.0/10'),    # Shared Address Space (CGN)
        ipaddress.ip_network('192.0.0.0/24'),     # IETF Protocol Assignments
        ipaddress.ip_network('192.0.2.0/24'),     # Documentation (TEST-NET-1)
        ipaddress.ip_network('198.51.100.0/24'),  # Documentation (TEST-NET-2)
        ipaddress.ip_network('203.0.113.0/24'),   # Documentation (TEST-NET-3)
        ipaddress.ip_network('224.0.0.0/4'),      # Multicast
        ipaddress.ip_network('240.0.0.0/4'),      # Reserved for Future Use
        ipaddress.ip_network('255.255.255.255/32'),  # Limited Broadcast
    ]
    
    # IPv6
    PRIVATE_IPV6_NETWORKS = [
        ipaddress.ip_network('::1/128'),          # Loopback
        ipaddress.ip_network('::/128'),           # Unspecified
        ipaddress.ip_network('::ffff:0:0/96'),    # IPv4-mapped (需要检查映射的 IPv4)
        ipaddress.ip_network('fc00::/7'),         # Unique Local Address (ULA)
        ipaddress.ip_network('fe80::/10'),        # Link-Local
        ipaddress.ip_network('ff00::/8'),         # Multicast
        ipaddress.ip_network('100::/64'),         # Discard-Only
        ipaddress.ip_network('2001:db8::/32'),    # Documentation
        ipaddress.ip_network('2001::/32'),        # Teredo (可能被滥用)
    ]
    
    # 特别危险的 IP 地址（云服务元数据端点等）
    DANGEROUS_IPS = {
        '169.254.169.254',  # AWS, GCP, Azure 等云服务元数据
        '169.254.170.2',    # AWS ECS Task Metadata
        'fd00:ec2::254',    # AWS EC2 IPv6 元数据
    }
    
    # 危险的主机名
    DANGEROUS_HOSTNAMES = {
        'localhost',
        'localhost.localdomain',
        'ip6-localhost',
        'ip6-loopback',
        'metadata.google.internal',
        'metadata.goog',
        'kubernetes.default',
        'kubernetes.default.svc',
        'kubernetes.default.svc.cluster.local',
    }
    
    def __init__(
        self,
        allow_private_network: bool = False,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None
    ):
        """初始化 URL 验证器
        
        Args:
            allow_private_network: 是否允许访问私有网络地址
            allowed_domains: 允许的域名白名单（支持通配符如 *.example.com）
            blocked_domains: 拒绝的域名黑名单（支持通配符）
        """
        self.allow_private_network = allow_private_network
        self.allowed_domains = allowed_domains or []
        self.blocked_domains = blocked_domains or []
        
        # 预编译域名匹配模式
        self._allowed_patterns = [self._compile_domain_pattern(d) for d in self.allowed_domains]
        self._blocked_patterns = [self._compile_domain_pattern(d) for d in self.blocked_domains]
    
    @staticmethod
    def _compile_domain_pattern(domain: str) -> str:
        """将域名模式转换为正则表达式模式
        
        支持的通配符：
        - *.example.com 匹配 sub.example.com, a.b.example.com 等
        - example.* 匹配 example.com, example.org 等
        """
        # 转义特殊字符，但保留 *
        pattern = re.escape(domain)
        # 将 \* 转换为 .* (匹配任意字符)
        pattern = pattern.replace(r'\*', r'[^/]*')
        # 确保完整匹配
        return f'^{pattern}$'
    
    def _match_domain_pattern(self, hostname: str, patterns: List[str]) -> bool:
        """检查主机名是否匹配任一模式"""
        hostname_lower = hostname.lower()
        for pattern in patterns:
            if re.match(pattern, hostname_lower, re.IGNORECASE):
                return True
        return False
    
    def _is_private_ip(self, ip_str: str) -> Tuple[bool, str]:
        """检查 IP 是否为私有/保留地址
        
        Args:
            ip_str: IP 地址字符串
            
        Returns:
            Tuple[bool, str]: (是否私有, 原因描述)
        """
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True, f"无效的 IP 地址: {ip_str}"
        
        # 检查特别危险的 IP
        if ip_str in self.DANGEROUS_IPS:
            return True, f"拒绝访问危险 IP（云服务元数据端点）: {ip_str}"
        
        # 检查 IPv4
        if isinstance(ip, ipaddress.IPv4Address):
            for network in self.PRIVATE_IPV4_NETWORKS:
                if ip in network:
                    return True, f"拒绝访问私有/保留 IPv4 地址: {ip_str} (属于 {network})"
            return False, ""
        
        # 检查 IPv6
        if isinstance(ip, ipaddress.IPv6Address):
            # 检查 IPv4-mapped IPv6 地址
            if ip.ipv4_mapped:
                is_private, reason = self._is_private_ip(str(ip.ipv4_mapped))
                if is_private:
                    return True, f"IPv4-mapped IPv6 地址包含私有 IP: {reason}"
            
            for network in self.PRIVATE_IPV6_NETWORKS:
                if ip in network:
                    return True, f"拒绝访问私有/保留 IPv6 地址: {ip_str} (属于 {network})"
            return False, ""
        
        return False, ""
    
    async def _resolve_hostname(self, hostname: str) -> List[str]:
        """异步解析主机名到 IP 地址列表
        
        Args:
            hostname: 主机名
            
        Returns:
            IP 地址列表
        """
        loop = asyncio.get_event_loop()
        try:
            # 使用 getaddrinfo 获取所有地址（IPv4 和 IPv6）
            result = await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            )
            # 提取唯一的 IP 地址
            ips = set()
            for family, socktype, proto, canonname, sockaddr in result:
                ips.add(sockaddr[0])
            return list(ips)
        except socket.gaierror as e:
            raise URLValidationError(f"DNS 解析失败: {hostname} - {e}")
        except Exception as e:
            raise URLValidationError(f"DNS 解析出错: {hostname} - {e}")
    
    async def validate_url(self, url: str) -> Tuple[bool, str]:
        """验证 URL 是否安全
        
        Args:
            url: 要验证的 URL
            
        Returns:
            Tuple[bool, str]: (是否安全, 消息描述)
        """
        # 解析 URL
        try:
            parsed = urlparse(url)
        except Exception as e:
            return False, f"URL 解析失败: {e}"
        
        # 1. 验证 scheme
        scheme = parsed.scheme.lower()
        if not scheme:
            return False, "URL 缺少协议 (scheme)，需要 http:// 或 https://"
        
        if scheme not in self.ALLOWED_SCHEMES:
            return False, f"不允许的 URL 协议: {scheme}。只允许 http 和 https"
        
        # 2. 获取主机名
        hostname = parsed.hostname
        if not hostname:
            return False, "URL 缺少主机名"
        
        hostname_lower = hostname.lower()
        
        # 3. 检查是否为 IP 地址格式
        is_ip = False
        try:
            ipaddress.ip_address(hostname)
            is_ip = True
        except ValueError:
            pass
        
        # 4. 检查黑名单（优先级最高）
        if self._match_domain_pattern(hostname_lower, self._blocked_patterns):
            return False, f"域名 {hostname} 在黑名单中"
        
        # 5. 检查危险主机名
        if hostname_lower in self.DANGEROUS_HOSTNAMES:
            if not self.allow_private_network:
                return False, f"拒绝访问危险主机名: {hostname}"
        
        # 6. 检查白名单（如果配置了白名单，则只允许白名单中的域名）
        if self._allowed_patterns:
            if not self._match_domain_pattern(hostname_lower, self._allowed_patterns):
                return False, f"域名 {hostname} 不在白名单中"
        
        # 7. 如果是 IP 地址，直接检查
        if is_ip:
            if not self.allow_private_network:
                is_private, reason = self._is_private_ip(hostname)
                if is_private:
                    return False, reason
            return True, "URL 验证通过"
        
        # 8. DNS 解析并检查所有解析到的 IP
        if not self.allow_private_network:
            try:
                ips = await self._resolve_hostname(hostname)
                if not ips:
                    return False, f"DNS 解析未返回任何 IP 地址: {hostname}"
                
                for ip in ips:
                    is_private, reason = self._is_private_ip(ip)
                    if is_private:
                        return False, f"域名 {hostname} 解析到私有 IP: {reason}"
                        
            except URLValidationError as e:
                return False, str(e)
            except Exception as e:
                logger.warning(f"DNS 解析警告 ({hostname}): {e}")
                # DNS 解析失败时，如果不是私有主机名，允许继续
                # Playwright 会再次尝试解析
        
        return True, "URL 验证通过"
    
    def validate_url_sync(self, url: str) -> Tuple[bool, str]:
        """同步版本的 URL 验证（不进行 DNS 解析）
        
        用于快速预检查，不涉及网络操作。
        
        Args:
            url: 要验证的 URL
            
        Returns:
            Tuple[bool, str]: (是否通过预检, 消息描述)
        """
        # 解析 URL
        try:
            parsed = urlparse(url)
        except Exception as e:
            return False, f"URL 解析失败: {e}"
        
        # 1. 验证 scheme
        scheme = parsed.scheme.lower()
        if not scheme:
            return False, "URL 缺少协议 (scheme)，需要 http:// 或 https://"
        
        if scheme not in self.ALLOWED_SCHEMES:
            return False, f"不允许的 URL 协议: {scheme}。只允许 http 和 https"
        
        # 2. 获取主机名
        hostname = parsed.hostname
        if not hostname:
            return False, "URL 缺少主机名"
        
        hostname_lower = hostname.lower()
        
        # 3. 检查黑名单
        if self._match_domain_pattern(hostname_lower, self._blocked_patterns):
            return False, f"域名 {hostname} 在黑名单中"
        
        # 4. 检查危险主机名
        if hostname_lower in self.DANGEROUS_HOSTNAMES:
            if not self.allow_private_network:
                return False, f"拒绝访问危险主机名: {hostname}"
        
        # 5. 检查白名单
        if self._allowed_patterns:
            if not self._match_domain_pattern(hostname_lower, self._allowed_patterns):
                return False, f"域名 {hostname} 不在白名单中"
        
        # 6. 如果是 IP 地址，检查是否为私有 IP
        try:
            ipaddress.ip_address(hostname)
            if not self.allow_private_network:
                is_private, reason = self._is_private_ip(hostname)
                if is_private:
                    return False, reason
        except ValueError:
            pass  # 不是 IP 地址，跳过
        
        return True, "预检通过（DNS 验证将在导航时进行）"


# 创建默认验证器实例
default_validator = URLValidator()


async def validate_browser_url(
    url: str,
    allow_private_network: bool = False,
    allowed_domains: Optional[List[str]] = None,
    blocked_domains: Optional[List[str]] = None
) -> Tuple[bool, str]:
    """验证浏览器要访问的 URL 是否安全
    
    便捷函数，用于一次性验证。
    
    Args:
        url: 要验证的 URL
        allow_private_network: 是否允许访问私有网络
        allowed_domains: 允许的域名白名单
        blocked_domains: 拒绝的域名黑名单
        
    Returns:
        Tuple[bool, str]: (是否安全, 消息描述)
    """
    validator = URLValidator(
        allow_private_network=allow_private_network,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains
    )
    return await validator.validate_url(url)