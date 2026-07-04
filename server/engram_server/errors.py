class KBError(ValueError):
    """Any knowledge-base misuse; the message teaches the calling model the correct next step."""


class GitError(KBError):
    """A git operation failed; the checkout is always left clean (never mid-rebase)."""


class FrontmatterError(KBError):
    """The concept's YAML frontmatter is missing or invalid."""
