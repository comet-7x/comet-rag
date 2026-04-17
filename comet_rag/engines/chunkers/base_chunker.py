# 引用自 https://github.com/crewAIInc/crewAI/blob/main/lib/crewai-tools/src/crewai_tools/rag/chunkers/
from collections import deque
from enum import StrEnum

from comet_rag.engines.chunkers.separators import (
    SEPARATORS_CODE_C,
    SEPARATORS_CODE_CPP,
    SEPARATORS_CODE_GO,
    SEPARATORS_CODE_HTML,
    SEPARATORS_CODE_JAVA,
    SEPARATORS_CODE_JS,
    SEPARATORS_CODE_PHP,
    SEPARATORS_CODE_PY,
    SEPARATORS_CODE_R,
    SEPARATORS_CODE_RUST,
    SEPARATORS_CODE_TS,
    SEPARATORS_EN,
    SEPARATORS_JA,
    SEPARATORS_KO,
    SEPARATORS_ZH,
)


class Language(StrEnum):
    ENGLISH = "en"
    CHINESE = "zh"
    JAPANESE = "ja"
    KOREAN = "ko"


class CodeLanguage(StrEnum):
    PY = "py"
    TS = "ts"
    JS = "js"
    JAVA = "java"
    C = "c"
    CPP = "cpp"
    GO = "go"
    PHP = "php"
    R = "r"
    RUST = "rust"
    HTML = "html"


# fmt: off
_LANGUAGE_TO_SEPARATORS: dict[Language | CodeLanguage, list[str]] = {
    Language.ENGLISH:    SEPARATORS_EN,
    Language.CHINESE:    SEPARATORS_ZH,
    Language.JAPANESE:   SEPARATORS_JA,
    Language.KOREAN:     SEPARATORS_KO,
    CodeLanguage.PY:     SEPARATORS_CODE_PY,
    CodeLanguage.TS:     SEPARATORS_CODE_TS,
    CodeLanguage.JS:     SEPARATORS_CODE_JS,
    CodeLanguage.JAVA:   SEPARATORS_CODE_JAVA,
    CodeLanguage.C:      SEPARATORS_CODE_C,
    CodeLanguage.CPP:    SEPARATORS_CODE_CPP,
    CodeLanguage.GO:     SEPARATORS_CODE_GO,
    CodeLanguage.PHP:    SEPARATORS_CODE_PHP,
    CodeLanguage.R:      SEPARATORS_CODE_R,
    CodeLanguage.RUST:   SEPARATORS_CODE_RUST,
    CodeLanguage.HTML:   SEPARATORS_CODE_HTML,
}
# fmt: on


class RecursiveCharacterTextSplitter:
    """基于分隔符层次结构递归拆分文本的文本分割器"""

    def __init__(
        self,
        chunk_size: int = 4000,
        chunk_overlap: int = 200,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ) -> None:
        """初始化 RecursiveCharacterTextSplitter

        Args:
            chunk_size (int): 每个 chunk 的最大字符数， 默认为 4000
            chunk_overlap (int): chunk 之间的重叠字符数， 默认为 200
            separators (list[str]): 分割符列表，按优先级排序
            keep_separator (bool): 是否在分割后的文本中保留分隔符, 默认为 `True`
        """
        if chunk_size <= 0:
            raise ValueError(
                f"{self.__class__.__name__} | chunk大小({chunk_size})必须大于0"
            )

        if chunk_overlap < 0:
            raise ValueError(
                f"{self.__class__.__name__} | 重叠字符数({chunk_overlap})必须大于等于0"
            )

        if chunk_overlap >= chunk_size:
            raise ValueError(
                f"{self.__class__.__name__} | 重叠字符数({chunk_overlap})不能大于等于chunk大小({chunk_size})"
            )

        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._keep_separator = keep_separator

        # 默认分隔符按优先级排序：双换行 -> 单换行 -> 空格 -> 逐字符
        self._separators = (
            separators
            if separators is not None
            else [
                "\n\n",
                "\n",
                " ",
                "",
            ]
        )

    def split_text(self, text: str) -> list[str]:
        """将输入文本拆分为 chunks

        Args:
            text: 要拆分的文本

        Returns:
            文本块列表
        """
        return self._split_text(text, self._separators)

    def _split_text(self, text: str, separators: list[str]) -> list[str]:
        """递归拆分文本的核心逻辑

        策略：优先用高优先级分隔符（如\\n\\n）拆分，如果结果仍超过 chunk_size，
        则用次优先级分隔符继续拆分，直到可以放入 chunk_size 或无法再分时按字符数强制拆分。

        每层只负责合并本层收集到的小片段；过大的片段递归处理后直接追加到输出，
        不再经过本层的 _merge_splits，避免双重合并。
        """
        final_chunks: list[str] = []

        separator = separators[-1]
        new_separators: list[str] = []

        # 从高优先级分隔符开始，找到第一个在文本中存在的分隔符
        for i, sep in enumerate(separators):
            if sep == "":
                separator = sep
                break
            if sep in text:
                separator = sep
                new_separators = separators[i + 1 :]
                break

        splits = self._split_text_with_separator(text, separator)

        # keep_separator=True 时分隔符已内嵌于片段，合并时直接拼接；
        # keep_separator=False 时需用原分隔符重新插入
        merge_sep = "" if self._keep_separator else separator

        good_splits: list[str] = []
        for split in splits:
            if len(split) <= self._chunk_size:
                good_splits.append(split)
            else:
                # 先将已积累的小片段合并输出，再处理当前过大的片段
                if good_splits:
                    final_chunks.extend(self._merge_splits(good_splits, merge_sep))
                    good_splits = []
                if new_separators:
                    # 递归结果直接追加，不再经过本层合并
                    final_chunks.extend(self._split_text(split, new_separators))
                else:
                    final_chunks.extend(self._split_by_characters(split))

        if good_splits:
            final_chunks.extend(self._merge_splits(good_splits, merge_sep))

        return final_chunks

    def _split_text_with_separator(self, text: str, separator: str) -> list[str]:
        """使用指定分隔符拆分文本，并可选保留分隔符"""
        # 空分隔符按字符逐个拆分
        if separator == "":
            return list(text)

        if self._keep_separator and separator in text:
            # 保留分隔符：将分隔符追加到其所属片段的末尾（而非前置到下一片段），
            # 保证每个内容单元的完整性（如句子以标点结尾，JSON 对象含闭合符号）
            parts = text.split(separator)
            splits = []

            for i, part in enumerate(parts):
                if i < len(parts) - 1:  # 非末尾片段：追加分隔符
                    if part:
                        splits.append(part + separator)
                    elif splits:
                        splits[-1] += separator  # 连续分隔符：并入上一段
                    else:  # 开头空片段：添加分隔符
                        splits.append(separator)
                elif part:  # 末尾片段：无后续分隔符
                    splits.append(part)
            return splits
        return text.split(separator)

    def _split_by_characters(self, text: str) -> list[str]:
        """按固定字符数强制拆分（用于无法再分割时的兜底）

        结果直接追加到输出，不经过 _merge_splits，因此不加 overlap。
        """
        chunks = []
        for i in range(0, len(text), self._chunk_size):
            chunks.append(text[i : i + self._chunk_size])
        return chunks

    def _merge_splits(self, splits: list[str], separator: str) -> list[str]:
        """将拆分结果合并为带重叠的 chunks

        处理逻辑：遍历 splits，当累积内容超过 chunk_size 时，
        将当前文档作为一个 chunk，并保留部分重叠内容到下一个 chunk
        """
        docs: list[str] = []
        current_doc: deque[str] = deque()
        total = 0

        for split in splits:
            split_len = len(split)
            # keep_separator=False 时新 split 加入后还需插入一个分隔符，需计入长度
            len_to_add = (
                split_len + len(separator)
                if not self._keep_separator and separator != "" and current_doc
                else split_len
            )

            # 累积内容已满，输出当前 chunk
            if total + len_to_add > self._chunk_size and current_doc:
                doc = separator.join(current_doc)
                if doc:
                    docs.append(doc)

                # 缩小窗口：满足 overlap 约束，同时确保新 split 能放入
                while current_doc and (
                    total > self._chunk_overlap or total + len_to_add > self._chunk_size
                ):
                    removed = current_doc.popleft()
                    total -= len(removed)
                    if not self._keep_separator and separator != "" and current_doc:
                        total -= len(separator)

                if not current_doc:
                    total = 0

            current_doc.append(split)
            total += split_len
            if not self._keep_separator and separator != "" and len(current_doc) > 1:
                total += len(separator)

        # 处理最后一个文档
        if current_doc:
            doc = ("" if self._keep_separator else separator).join(current_doc)
            if doc:
                docs.append(doc)

        return docs


class BaseChunker:
    """文本分块器基类，对外提供统一的 chunk 接口"""

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        language: Language | CodeLanguage | None = Language.ENGLISH,
    ) -> None:
        """初始化 Chunker

        Args:
            chunk_size (int): 每个 chunk 的最大字符数， 默认 1000
            chunk_overlap (int): 相邻 chunk 之间的重叠字符数， 默认 200
            separators (list[str]): 分割符列表，按优先级排序
            keep_separator (bool): 是否保留分隔符， 默认 `True`
            language (Language | CodeLanguage): 语言，可以是国家语言或者代码语言，默认为国家语言 `Language.ENGLISH`
        """
        if separators is None and language:
            separators = _LANGUAGE_TO_SEPARATORS.get(language)

        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=separators,
            keep_separator=keep_separator,
        )

    def chunk(self, text: str) -> list[str]:
        """将文本分块

        Args:
            text: 要分块的文本

        Returns:
            分块后的文本列表
        """
        if not text or not text.strip():
            return []

        return self._splitter.split_text(text)
