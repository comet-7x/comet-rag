from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from comet_rag.engines.documents.mineru.structured import decode_json


@dataclass(frozen=True, slots=True, order=True)
class MinioBuild:
    version: str
    commit: str


def server_builds(raw: bytes) -> tuple[MinioBuild, ...]:
    """只消费官方mc的只读JSON，白名单输出构建身份；不持久化拓扑或管理信息。"""
    if len(raw) > 512 * 1024:
        raise ValueError("管理诊断超过512KiB")
    value = decode_json(raw)
    info = value.get("info")
    if value.get("status") != "success" or not isinstance(info, dict):
        raise ValueError("mc管理查询未成功")
    nodes = info.get("servers")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 128:
        raise ValueError("服务节点清单无效或超出诊断预算")
    builds: set[MinioBuild] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("服务构建结构无效")
        version, commit = node.get("version"), node.get("commitID")
        if not isinstance(version, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", version
        ):
            raise ValueError("服务没有可核对的版本时间戳")
        datetime.strptime(version, "%Y-%m-%dT%H:%M:%SZ")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("服务没有可核对的提交号")
        builds.add(MinioBuild(version, commit))
    # 保留混部事实，不擅自选择一个最新版本代表整个部署。
    return tuple(sorted(builds))


@dataclass(frozen=True, slots=True)
class SignedCondition:
    sigv4: bool
    star: bool
    signed: bool


def signed_condition(headers: Mapping[str, str | bytes]) -> SignedCondition:
    """用于SDK before-send钩子；结果不含Authorization、AK、签名、URL或正文。"""
    fields: dict[str, str | bytes] = {}
    for key, value in headers.items():
        name = key.lower()
        if name in fields:
            raise ValueError("诊断请求头重复，不能判断签名事实")
        fields[name] = value

    def text(name: str) -> str:
        value = fields.get(name, "")
        if isinstance(value, bytes):
            try:
                return value.decode("ascii")
            except UnicodeError:
                raise ValueError("诊断请求头编码无效") from None
        return value

    authorization = text("authorization")
    sigv4 = authorization.startswith("AWS4-HMAC-SHA256 ")
    matches = re.findall(r"(?:^|[ ,])SignedHeaders=([^, ]+)", authorization)
    if len(matches) > 1:
        raise ValueError("签名头声明重复")
    names = matches[0].split(";") if matches else []
    if len(set(names)) != len(names) or any(
        not re.fullmatch(r"[a-z0-9-]+", name) for name in names
    ):
        raise ValueError("签名头声明无效")
    star = text("if-none-match") == "*"
    return SignedCondition(sigv4, star, sigv4 and "if-none-match" in names)


def header_tamper_rejected(
    original: SignedCondition,
    *,
    baseline_status: int,
    stripped: tuple[int, str],
    altered: tuple[int, str],
) -> bool:
    """支持排除本次简单丢头解释，不认证不存在代理、未重签名或全部路由一致。"""
    return (
        original.sigv4
        and original.star
        and original.signed
        and baseline_status in {200, 404, 412}
        and stripped in {(400, "AccessDenied"), (403, "SignatureDoesNotMatch")}
        and altered == (403, "SignatureDoesNotMatch")
    )
