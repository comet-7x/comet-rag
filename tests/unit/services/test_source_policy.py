"""入库来源准入策略（PR 评审 #4）。

被拦下的两类攻击都在这里各有一条用例：

    {"source": "/etc/passwd"}                    → 任意文件读取
    {"source": "http://169.254.169.254/..."}     → SSRF（云元数据里有临时凭据）

DNS 解析是注入进来的，所以整套用例**不打网络**，跟着单元测试跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comet_rag.services.source_policy import (
    SourceNotAllowed,
    SourcePolicy,
    build_source_policy,
)


def policy(**overrides) -> SourcePolicy:
    """默认解析成一个公网地址；要测私网就覆盖 `resolve`。"""
    defaults = {"resolve": lambda host: ["93.184.216.34"]}
    return SourcePolicy(**{**defaults, **overrides})


# ── 默认是拒绝 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        "/etc/passwd",
        "/etc/shadow",
        "../../../../etc/passwd",
        "~/.ssh/id_rsa",
        "relative/path.docx",
        "C:\\Windows\\win.ini",
    ],
)
def test_local_paths_are_denied_by_default(source: str) -> None:
    """**默认不许读服务器本地文件。**

    开着的话，调用方能让服务把它权限内的任意文件切块入库，
    然后从 `/search` 读出来 —— 一个完整的任意文件读取通道。
    """
    with pytest.raises(SourceNotAllowed, match="未开放"):
        policy().check(source)


def test_local_paths_pass_once_explicitly_allowed() -> None:
    """单机部署确实需要这个能力，所以是可配的 —— 只是必须显式打开。"""
    policy(allow_local=True).check("/tmp/report.docx")  # noqa: S108


def test_local_roots_confine_the_filesystem(tmp_path: Path) -> None:
    allowed = tmp_path / "corpus"
    allowed.mkdir()
    p = policy(allow_local=True, local_roots=[str(allowed)])

    p.check(str(allowed / "a.docx"))
    p.check(str(allowed / "nested" / "b.docx"))
    with pytest.raises(SourceNotAllowed, match="不在允许的根目录"):
        p.check(str(tmp_path / "outside.docx"))


def test_dot_dot_cannot_escape_the_root(tmp_path: Path) -> None:
    """`..` 必须在**解析之后**再比对，否则包含性检查形同虚设。"""
    allowed = tmp_path / "corpus"
    allowed.mkdir()
    p = policy(allow_local=True, local_roots=[str(allowed)])

    with pytest.raises(SourceNotAllowed, match="不在允许的根目录"):
        p.check(str(allowed / ".." / ".." / "etc" / "passwd"))


def test_symlink_cannot_escape_the_root(tmp_path: Path) -> None:
    """符号链接是绕过目录白名单最经典的手法。"""
    allowed = tmp_path / "corpus"
    allowed.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("s", encoding="utf-8")
    link = allowed / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:  # pragma: no cover —— 少数环境不允许建符号链接
        pytest.skip("当前环境不支持符号链接")

    with pytest.raises(SourceNotAllowed, match="不在允许的根目录"):
        policy(allow_local=True, local_roots=[str(allowed)]).check(str(link))


# ── SSRF ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("host", "address"),
    [
        ("169.254.169.254", "169.254.169.254"),  # 云元数据服务：临时凭据
        ("localhost", "127.0.0.1"),
        ("internal.corp", "10.0.0.5"),
        ("db.internal", "192.168.1.10"),
        ("svc.cluster.local", "172.16.0.9"),
        ("nat.example", "100.64.0.1"),  # CGNAT，容易被漏掉
        ("v6.example", "::1"),
        ("v6priv.example", "fd00::1"),
    ],
)
def test_private_targets_are_rejected(host: str, address: str) -> None:
    """**默认挡住私网/环回/链路本地/保留段。**

    这一条不需要用户配置就能挡住绝大多数 SSRF，而正常的公网抓取不受影响。
    """
    with pytest.raises(SourceNotAllowed, match="非公网"):
        policy(resolve=lambda h: [address]).check(f"http://{host}/x")


def test_public_urls_pass() -> None:
    policy().check("https://example.com/report.docx")


def test_dns_returning_a_mixed_set_is_rejected() -> None:
    """**所有**解析结果都必须是公网地址。

    只查第一条会被 DNS 轮询绕过：同一个域名可以同时返回一个公网 IP
    和一个内网 IP，攻击者只需多试几次。
    """
    with pytest.raises(SourceNotAllowed, match="非公网"):
        policy(resolve=lambda h: ["93.184.216.34", "10.0.0.5"]).check(
            "http://mixed.example/x"
        )


def test_unresolvable_host_is_rejected() -> None:
    with pytest.raises(SourceNotAllowed, match="无法解析"):
        policy(resolve=lambda h: []).check("http://nope.invalid/x")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/1", "ftp://h/f"])
def test_non_http_schemes_are_rejected(url: str) -> None:
    """`file://` 尤其要挡：它是绕开"本地路径"检查的常见写法。"""
    with pytest.raises(SourceNotAllowed):
        policy().check(url)


def test_redirects_are_validated_per_hop() -> None:
    """**只校验入口 URL 挡不住 SSRF**：公网地址可以 302 到 169.254.169.254，
    而 httpx 默认跟随重定向。"""
    p = policy(resolve=lambda h: ["169.254.169.254"])
    with pytest.raises(SourceNotAllowed, match="非公网"):
        p.check_redirect("http://evil.example/redirected")


def test_host_allowlist_narrows_further() -> None:
    p = policy(allowed_url_hosts=["files.example.com"])
    p.check("https://files.example.com/a.docx")
    with pytest.raises(SourceNotAllowed, match="不在允许列表"):
        p.check("https://other.example.com/a.docx")


def test_private_network_can_be_opened_deliberately() -> None:
    """内网部署确实可能需要抓内网地址 —— 可以开，但要显式开。"""
    policy(allow_private_network=True, resolve=lambda h: ["10.0.0.5"]).check(
        "http://internal/x"
    )


def test_empty_source_is_rejected() -> None:
    with pytest.raises(SourceNotAllowed):
        policy().check("   ")


# ── S3 / MinIO ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source", ["s3://documents/report.txt", "minio://documents/report.txt"]
)
def test_object_storage_is_denied_by_default(source: str) -> None:
    with pytest.raises(SourceNotAllowed, match="未开放"):
        policy().check(source)


def test_object_storage_requires_explicit_bucket_access() -> None:
    p = policy(allow_s3=True, allowed_s3_buckets=["documents"])

    p.check("s3://documents/report.txt")
    p.check("minio://DOCUMENTS/report.txt")
    with pytest.raises(SourceNotAllowed, match="bucket 不在允许列表"):
        p.check("s3://secrets/report.txt")


@pytest.mark.parametrize(
    "source",
    [
        "s3:///report.txt",
        "s3://documents",
        "s3://user:secret@documents/report.txt",
        "s3://documents:9000/report.txt",
        "s3://documents/report.txt?versionId=1",
    ],
)
def test_malformed_object_storage_uris_are_rejected(source: str) -> None:
    with pytest.raises(SourceNotAllowed):
        policy(allow_s3=True).check(source)


# ── 装配 ───────────────────────────────────────────────────────────────────


def test_build_warns_when_local_is_wide_open() -> None:
    """把危险开关打开这件事必须留痕，否则事后查不出"谁什么时候开的"。"""
    from comet_rag.config.schemas import IngestPolicyConfig
    from comet_rag.core.logging import logger

    records: list[str] = []
    sink = logger.add(lambda m: records.append(m.record["message"]), level="WARNING")
    try:
        built = build_source_policy(IngestPolicyConfig(allow_local=True))
    finally:
        logger.remove(sink)

    assert built.allow_local is True
    assert any("未限定根目录" in r for r in records), f"没有告警：{records}"


def test_defaults_are_locked_down() -> None:
    """回归守卫：默认值被谁改松了，这里立刻红。"""
    from comet_rag.config.schemas import IngestPolicyConfig

    built = build_source_policy(IngestPolicyConfig())
    assert built.allow_local is False
    assert built.allow_private_network is False
    assert built.allow_s3 is False


def test_build_warns_when_object_storage_has_no_bucket_allowlist() -> None:
    from comet_rag.config.schemas import IngestPolicyConfig
    from comet_rag.core.logging import logger

    records: list[str] = []
    sink = logger.add(lambda m: records.append(m.record["message"]), level="WARNING")
    try:
        built = build_source_policy(IngestPolicyConfig(allow_s3=True))
    finally:
        logger.remove(sink)

    assert built.allow_s3 is True
    assert any("未限定 bucket" in record for record in records)
