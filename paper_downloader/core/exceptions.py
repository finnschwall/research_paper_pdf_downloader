from __future__ import annotations


class PipelineError(Exception):
    pass


class ConfigurationError(PipelineError):
    pass


class InputParseError(PipelineError):
    pass


class MetadataError(PipelineError):
    pass


class IdentifierRecoveryError(PipelineError):
    pass


class ResolutionError(PipelineError):
    pass


class DownloadError(PipelineError):
    pass


class PDFValidationError(PipelineError):
    pass


class NotAPDFError(PDFValidationError):
    """The URL answered, but with a web page instead of a PDF.

    Carries the page so the caller can look for the real PDF link on it without paying for
    a second request. See paper_downloader.download.landing_page.
    """

    def __init__(self, message: str, *, final_url: str = "", body: str = "") -> None:
        super().__init__(message)
        self.final_url = final_url
        self.body = body


class HostBlockedError(DownloadError):
    """The host is refusing this server, not answering about this paper.

    Raised when a response looks like an edge-network denial or a bot challenge rather than
    a document -- see paper_downloader.core.host_gate. Kept apart from HTTPStatusError
    because the two mean opposite things to a caller deciding whether to try again: a 403
    on an article is a fact about the article, whereas this is a fact about us, and the
    paper is worth another attempt once the block ages out.
    """

    def __init__(self, message: str, *, host: str = "") -> None:
        super().__init__(message)
        self.host = host


class HTTPStatusError(DownloadError):
    """A download attempt came back with an HTTP error status.

    Keeps the status code so callers can tell a permanent refusal (401/403) from a
    transient one (5xx) instead of parsing it back out of the message.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ExtractionError(PipelineError):
    pass


class CaptioningError(PipelineError):
    pass


class ResumeError(PipelineError):
    pass


class ManifestStoreError(PipelineError):
    pass