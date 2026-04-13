# 引用自 https://github.com/crewAIInc/crewAI/blob/main/lib/crewai-tools/src/crewai_tools/rag/chunkers/
import re


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
            chunk_size: 每个 chunk 的最大字符数
            chunk_overlap: chunk 之间的重叠字符数
            separators: 分割符列表，按优先级排序
            keep_separator: 是否在分割后的文本中保留分隔符
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
        self._separators = separators or [
            "\n\n",
            "\n",
            " ",
            "",
        ]

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
        则用次优先级分隔符继续拆分，直到可以放入 chunk_size 或无法再分时按字符数强制拆分
        """
        separator = separators[-1]
        new_separators = []

        # 从高优先级分隔符开始，找到第一个在文本中存在的分隔符
        for i, sep in enumerate(separators):
            if sep == "":
                separator = sep
                break
            if re.search(re.escape(sep), text):
                separator = sep
                new_separators = separators[i + 1 :]
                break

        splits = self._split_text_with_separator(text, separator)

        good_splits = []

        for split in splits:
            # 如果当前片段小于 chunk_size，直接保留
            if len(split) < self._chunk_size:
                good_splits.append(split)
            else:
                # 否则用更低优先级的分隔符继续拆分
                if new_separators:
                    other_info = self._split_text(split, new_separators)
                    good_splits.extend(other_info)
                else:
                    # 没有更多分隔符时，按固定长度强制拆分
                    good_splits.extend(self._split_by_characters(split))

        return self._merge_splits(good_splits, separator)

    def _split_text_with_separator(self, text: str, separator: str) -> list[str]:
        """使用指定分隔符拆分文本，并可选保留分隔符"""
        # 空分隔符按字符逐个拆分
        if separator == "":
            return list(text)

        if self._keep_separator and separator in text:
            # 保留分隔符：在每个片段前加上分隔符（首尾除外）
            parts = text.split(separator)
            splits = []

            for i, part in enumerate(parts):
                if i == 0:
                    splits.append(part)
                elif i == len(parts) - 1:
                    if part:
                        splits.append(separator + part)
                else:
                    if part:
                        splits.append(separator + part)
                    else:
                        if splits:
                            splits[-1] += separator

            return [s for s in splits if s]
        return text.split(separator)

    def _split_by_characters(self, text: str) -> list[str]:
        """按固定字符数强制拆分（用于无法再分割时的兜底）"""
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
        current_doc: list[str] = []
        total = 0

        for split in splits:
            split_len = len(split)

            # 累积内容已满，输出当前 chunk
            if total + split_len > self._chunk_size and current_doc:
                if separator == "":
                    doc = "".join(current_doc)
                else:
                    if self._keep_separator and separator == " ":
                        doc = "".join(current_doc)
                    else:
                        doc = separator.join(current_doc)

                if doc:
                    docs.append(doc)

                # 实现 overlap：移除前面的片段，直到内容减少到 overlap 以下
                while total > self._chunk_overlap and len(current_doc) > 1:
                    removed = current_doc.pop(0)
                    total -= len(removed)
                    if separator != "":
                        total -= len(separator)

            current_doc.append(split)
            total += split_len
            if separator != "" and len(current_doc) > 1:
                total += len(separator)

        # 处理最后一个文档
        if current_doc:
            if separator == "":
                doc = "".join(current_doc)
            else:
                if self._keep_separator and separator == " ":
                    doc = "".join(current_doc)
                else:
                    doc = separator.join(current_doc)

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
    ) -> None:
        """初始化 Chunker

        Args:
            chunk_size: 每个 chunk 的最大字符数
            chunk_overlap: 相邻 chunk 之间的重叠字符数
            separators: 分割符列表，按优先级排序
            keep_separator: 是否保留分隔符
        """
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
