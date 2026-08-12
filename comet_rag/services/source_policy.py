"""入库来源准入策略（PR 评审 #4）。

## 为什么必须有它

`POST /ingest` 的 `source` 是调用方给的字符串，会被直接交给 `AutoLoader`：

    {"source": "/etc/passwd"}
        → LocalLoader 读它 → 内容切块入库 → 从 /search 就能读出来
    {"source": "http://169.254.169.254/latest/meta-data/iam/..."}
        → URLLoader 拉它 → 云上的临时凭据进了知识库

前者是**任意文件读取**，后者是**SSRF**。两者都以服务进程的身份和网络可达性
为界 —— 也就是说，服务能读到的、能连到的，调用方都能拿到。

## 默认是拒绝

`allow_local` 默认 **False**：服务端读本地文件是个危险能力，需要显式打开。
单机部署想用"把服务器上的文件入库"这个功能，就在配置里开，并用 `local_roots`
圈定范围 —— 显式声明比默认敞开安全得多。

URL 默认允许，但**私网、环回、链路本地、保留地址一律拒绝**。这条不需要
用户配置也能挡住绝大多数 SSRF，而正常的公网抓取不受影响。

## 重定向必须逐跳校验

`URLLoader` 默认跟随重定向，所以只查用户给的那个 URL 是不够的：
一个公网地址可以 302 到 `169.254.169.254`。策略对象因此同时提供
`check()`（入口校验）与 `check_redirect()`（逐跳校验），
后者由 loader 在跟随时调用。

## 层级

放在 `services/` 而不是 `engines/`：**当库用**的时候，用户读自己机器上任意
路径是完全正当的（spec A1 的"库"那一半）；只有把它做成**对外服务**时，
来源才需要被约束。策略属于服务的职责。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from comet_rag.core.logging import logger


class SourceNotAllowed(PermissionError):
    """来源被准入策略拒绝。API 层映射成 HTTP 403。

    继承 `PermissionError` 而不是 `ValueError`：它表达的是"不允许"，
    而不是"你写错了"，两者对应的 HTTP 语义与客户端处理方式都不同。
    """


def _is_public_ip(raw: str) -> bool:
    """这个 IP 是否属于可以对外访问的公网地址。

    `is_global` 一次覆盖私网、环回、链路本地、保留段与组播 ——
    自己逐段判断很容易漏掉 `0.0.0.0/8`、`100.64.0.0/10`（CGNAT）
    或 IPv6 的各种映射写法。
    """
    try:
        return ipaddress.ip_address(raw).is_global
    except ValueError:
        return False


@dataclass(slots=True)
class SourcePolicy:
    """决定一个 `source` 能不能被服务端加载。"""

    #: 是否允许读服务器本地文件。**默认关**，见模块文档。
    allow_local: bool = False
    #: 允许的本地根目录。为空时 `allow_local=True` 等于放开整个文件系统。
    local_roots: Sequence[str] = ()
    allow_url: bool = True
    #: 允许访问私网/环回地址。**默认关** —— 这是挡 SSRF 的主力。
    allow_private_network: bool = False
    #: URL 主机白名单。为空 = 不限（私网仍然被上一条挡着）。
    allowed_url_hosts: Sequence[str] = ()
    #: DNS 解析函数，可注入 —— 测试不必真的查 DNS。
    resolve: Callable[[str], list[str]] = field(default=lambda host: _resolve(host))

    # ── 入口校验 ───────────────────────────────────────────────────────────

    def check(self, source: str) -> None:
        """放行则静默返回，否则抛 `SourceNotAllowed`。"""
        raw = (source or "").strip()
        if not raw:
            raise SourceNotAllowed("来源不能为空")

        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            self._check_url(raw, parsed.scheme, parsed.hostname)
            return
        # 带 scheme 却没有 netloc（file:// 之类）一律拒绝：
        # 那既不是正常的本地路径写法，也是绕过 URL 检查的常见手法。
        if parsed.scheme and len(parsed.scheme) > 1:
            raise SourceNotAllowed(f"不支持的来源协议：{parsed.scheme}://")
        self._check_local(raw)

    def check_redirect(self, url: str) -> None:
        """重定向的每一跳都要重来一遍。

        只校验入口 URL 是挡不住 SSRF 的：公网地址可以 302 到 169.254.169.254，
        而 httpx 默认跟随重定向。
        """
        parsed = urlparse(url)
        self._check_url(url, parsed.scheme, parsed.hostname)

    # ── 分支 ───────────────────────────────────────────────────────────────

    def _check_url(self, url: str, scheme: str, host: str | None) -> None:
        if not self.allow_url:
            raise SourceNotAllowed("本服务未开放从 URL 入库")
        if scheme.lower() not in ("http", "https"):
            raise SourceNotAllowed(f"只支持 http/https，收到 {scheme}://")
        if not host:
            raise SourceNotAllowed(f"URL 缺少主机名：{url}")

        if self.allowed_url_hosts and host.lower() not in {
            h.lower() for h in self.allowed_url_hosts
        }:
            raise SourceNotAllowed(f"主机不在允许列表内：{host}")

        if self.allow_private_network:
            return

        addresses = self.resolve(host)
        if not addresses:
            raise SourceNotAllowed(f"无法解析主机：{host}")
        # **所有**解析结果都必须是公网地址。只查第一条会被 DNS 轮询绕过：
        # 同一个域名可以同时返回一个公网 IP 和一个内网 IP。
        private = [ip for ip in addresses if not _is_public_ip(ip)]
        if private:
            raise SourceNotAllowed(
                f"主机 {host} 解析到非公网地址 {private}，已拒绝（疑似 SSRF）"
            )

    def _check_local(self, raw: str) -> None:
        if not self.allow_local:
            raise SourceNotAllowed(
                "本服务未开放从服务器本地路径入库"
                "（如确需，请配置 ingest_policy.allow_local 与 local_roots）"
            )
        if not self.local_roots:
            return

        # `resolve()` 会展开符号链接与 `..`，否则 `roots/../../etc/passwd`
        # 这类写法能直接绕过包含性检查。
        target = Path(raw).expanduser().resolve()
        for root in self.local_roots:
            base = Path(root).expanduser().resolve()
            if target == base or base in target.parents:
                return
        raise SourceNotAllowed(f"路径不在允许的根目录内：{target}")


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return [info[4][0] for info in infos]


def build_source_policy(config: object) -> SourcePolicy:
    """从配置构造策略，并在放开危险开关时**大声说出来**。

    默认值是安全的，但配置能把它关掉；关掉这件事必须在启动日志里留痕，
    否则"谁什么时候把它打开的"事后无从追查。
    """
    policy = SourcePolicy(
        allow_local=getattr(config, "allow_local", False),
        local_roots=tuple(getattr(config, "local_roots", ()) or ()),
        allow_url=getattr(config, "allow_url", True),
        allow_private_network=getattr(config, "allow_private_network", False),
        allowed_url_hosts=tuple(getattr(config, "allowed_url_hosts", ()) or ()),
    )
    if policy.allow_local and not policy.local_roots:
        logger.warning(
            "入库策略：已允许读取服务器本地文件且**未限定根目录** —— "
            "调用方可以让服务读取它权限内的任意文件。生产环境请配置 local_roots。"
        )
    if policy.allow_private_network:
        logger.warning("入库策略：已允许访问私网地址，SSRF 防护被关闭")
    return policy


__all__ = ["SourceNotAllowed", "SourcePolicy", "build_source_policy"]
