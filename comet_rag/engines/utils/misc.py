import hashlib


def compute_sha256(content: str) -> str:
    """Compute the SHA-256 hash of the given content.

    Args:
      content: The content to hash.

    Returns:
      The SHA-256 hash of the content as a hexadecimal string.
    """
    return hashlib.sha256(content.encode()).hexdigest()
