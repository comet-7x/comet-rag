from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

from lxml import etree

from comet_rag.content import MediaRef
from comet_rag.documents.schemas import Asset

from ..archive import ArchiveLimits, _validate_xml_members, _validate_zip_metadata

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
V = "urn:schemas-microsoft-com:vml"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def q(name: str, namespace: str = W) -> str:
    return f"{{{namespace}}}{name}"


def val(element: etree._Element | None, default: str | None = None) -> str | None:
    return element.get(q("val"), default) if element is not None else default


def integer(value: str | None, name: str, *, minimum: int = 0) -> int:
    if value is None:
        raise ValueError(f"missing DOCX {name}")
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid DOCX {name}: {value!r}") from exc
    if number < minimum:
        raise ValueError(f"DOCX {name} must be >= {minimum}")
    return number


@dataclass(frozen=True, slots=True)
class Relationship:
    kind: str
    target: str
    external: bool = False


class Package:
    def __init__(
        self, archive: ZipFile, output_dir: Path, limits: ArchiveLimits
    ) -> None:
        self.archive = archive
        self.output_dir = output_dir
        members = archive.infolist()
        _validate_zip_metadata(members, limits)
        self.names: set[str] = set()
        for member in members:
            name = member.filename
            if name in self.names:
                raise ValueError(f"unsafe or duplicate DOCX archive member: {name!r}")
            if "\\" in name:
                raise ValueError(f"unsafe or duplicate DOCX archive member: {name!r}")
            if name.startswith("/"):
                raise ValueError(f"unsafe or duplicate DOCX archive member: {name!r}")
            if any(part in {".", ".."} for part in name.rstrip("/").split("/")):
                raise ValueError(f"unsafe or duplicate DOCX archive member: {name!r}")
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError(f"unsafe or duplicate DOCX archive member: {name!r}")
            self.names.add(name)
        self._limits = limits
        self._validated_xml = {
            member.filename
            for member in members
            if member.filename.lower().endswith((".xml", ".rels"))
        }
        self._xml_elements = _validate_xml_members(archive, members, limits)
        self._xml: dict[str, etree._Element] = {}
        self._relationships: dict[str, dict[str, Relationship]] = {}
        self.assets: dict[str, Asset] = {}
        self._defaults: dict[str, str] = {}
        self._overrides: dict[str, str] = {}
        root = self.xml("[Content_Types].xml", q("Types", CT))
        for item in root:
            if item.tag == q("Default", CT):
                key, target = item.get("Extension"), self._defaults
            elif item.tag == q("Override", CT):
                key, target = item.get("PartName"), self._overrides
                key = key.lstrip("/") if key else key
            else:
                raise ValueError("invalid DOCX content type declaration")
            media_type = item.get("ContentType")
            if not key:
                raise ValueError("invalid or duplicate DOCX content type")
            if not media_type:
                raise ValueError("invalid or duplicate DOCX content type")
            if key in target:
                raise ValueError("invalid or duplicate DOCX content type")
            target[key] = media_type
        mains = [
            item
            for item in self.relationships("").values()
            if item.kind == f"{R}/officeDocument"
        ]
        if len(mains) != 1:
            raise ValueError("DOCX requires one internal officeDocument relationship")
        if mains[0].external:
            raise ValueError("DOCX requires one internal officeDocument relationship")
        self.main = mains[0].target
        if (
            self.media_type(self.main)
            != "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        ):
            raise ValueError("resource is not a native DOCX document")

    def xml(self, name: str, expected: str | None = None) -> etree._Element:
        if name not in self._xml:
            if name not in self.names:
                raise ValueError(f"missing DOCX part: {name}")
            if name not in self._validated_xml:
                self._xml_elements = _validate_xml_members(
                    self.archive,
                    [self.archive.getinfo(name)],
                    self._limits,
                    xml_names={name},
                    element_count=self._xml_elements,
                )
                self._validated_xml.add(name)
            root = etree.fromstring(
                self.archive.read(name),
                parser=etree.XMLParser(
                    resolve_entities=False,
                    no_network=True,
                    load_dtd=False,
                    huge_tree=False,
                ),
            )
            if root.getroottree().docinfo.internalDTD is not None:
                raise ValueError(f"DTD and entities are not supported in DOCX: {name}")
            if any(isinstance(node, etree._Entity) for node in root.iter()):
                raise ValueError(f"DTD and entities are not supported in DOCX: {name}")
            self._xml[name] = root
            references = [
                rid
                for node in root.iter()
                for attribute, rid in node.attrib.items()
                if attribute in {q("id", R), q("embed", R), q("link", R)}
            ]
            if references:
                relationships = self.relationships(name)
                if any(rid not in relationships for rid in references):
                    raise ValueError(f"missing DOCX resource relationship in {name}")
        root = self._xml[name]
        if expected is not None:
            if root.tag != expected:
                raise ValueError(f"unexpected DOCX XML root in {name}")
        return root

    def relationships(self, part: str) -> dict[str, Relationship]:
        if part in self._relationships:
            return self._relationships[part]
        name = (
            posixpath.join(
                posixpath.dirname(part), "_rels", f"{posixpath.basename(part)}.rels"
            )
            if part
            else "_rels/.rels"
        )
        result: dict[str, Relationship] = {}
        if name in self.names:
            for item in self.xml(name, q("Relationships", RELS)):
                rid, kind, target = item.get("Id"), item.get("Type"), item.get("Target")
                mode = item.get("TargetMode", "Internal")
                if item.tag != q("Relationship", RELS):
                    raise ValueError(f"invalid DOCX relationship in {name}")
                if not rid:
                    raise ValueError(f"invalid DOCX relationship in {name}")
                if not kind:
                    raise ValueError(f"invalid DOCX relationship in {name}")
                if not target:
                    raise ValueError(f"invalid DOCX relationship in {name}")
                if rid in result:
                    raise ValueError(f"invalid DOCX relationship in {name}")
                if mode not in {"Internal", "External"}:
                    raise ValueError(f"invalid DOCX relationship in {name}")
                external = mode == "External"
                if not external:
                    parsed = urlsplit(target)
                    if parsed.scheme:
                        raise ValueError("invalid internal DOCX relationship target")
                    if parsed.netloc:
                        raise ValueError("invalid internal DOCX relationship target")
                    if parsed.query:
                        raise ValueError("invalid internal DOCX relationship target")
                    if parsed.fragment:
                        raise ValueError("invalid internal DOCX relationship target")
                    if "\\" in target:
                        raise ValueError("invalid internal DOCX relationship target")
                    decoded = unquote(parsed.path)
                    target = posixpath.normpath(
                        decoded.lstrip("/")
                        if decoded.startswith("/")
                        else posixpath.join(posixpath.dirname(part), decoded)
                    )
                    if target.startswith("../"):
                        raise ValueError(
                            f"missing or unsafe DOCX relationship target: {target}"
                        )
                    if target not in self.names:
                        raise ValueError(
                            f"missing or unsafe DOCX relationship target: {target}"
                        )
                result[rid] = Relationship(kind, target, external)
        self._relationships[part] = result
        return result

    def related(self, part: str, rid: str | None, kind: str) -> Relationship:
        relation = self.relationships(part).get(rid or "")
        if relation is None:
            raise ValueError(f"missing or invalid DOCX {kind} relationship: {rid}")
        if relation.kind != f"{R}/{kind}":
            raise ValueError(f"missing or invalid DOCX {kind} relationship: {rid}")
        return relation

    def media_type(self, part: str) -> str | None:
        return self._overrides.get(part) or self._defaults.get(
            PurePosixPath(part).suffix.lstrip(".")
        )

    def image(self, relation: Relationship) -> Asset:
        if relation.external:
            raise ValueError("external image has no local bytes")
        data = self.archive.read(relation.target)
        if not data:
            raise ValueError("empty DOCX image resource")
        content_hash = hashlib.sha256(data).hexdigest()
        if content_hash in self.assets:
            return self.assets[content_hash]
        media_type = self.media_type(relation.target)
        if media_type is None:
            raise ValueError("DOCX image has no image content type")
        if not media_type.startswith("image/"):
            raise ValueError("DOCX image has no image content type")
        extension = mimetypes.guess_extension(media_type) or ".bin"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir.resolve() / f"{content_hash}{extension}"
        try:
            with destination.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            if destination.is_symlink():
                raise ValueError(
                    f"asset output conflicts with existing file: {destination}"
                ) from None
            if not destination.is_file():
                raise ValueError(
                    f"asset output conflicts with existing file: {destination}"
                ) from None
            if destination.stat().st_size != len(data):
                raise ValueError(
                    f"asset output conflicts with existing file: {destination}"
                ) from None
            if destination.read_bytes() != data:
                raise ValueError(
                    f"asset output conflicts with existing file: {destination}"
                ) from None
        asset = Asset(
            asset_id=f"asset_{content_hash}",
            content_hash=content_hash,
            media=MediaRef(path=destination, media_type=media_type),
            media_type=media_type,
            name=PurePosixPath(relation.target).name,
            size_bytes=len(data),
            provenance="extracted",
        )
        self.assets[content_hash] = asset
        return asset
