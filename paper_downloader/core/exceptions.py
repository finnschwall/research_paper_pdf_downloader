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


class WrongPaperError(PDFValidationError):
    """A real PDF arrived, and it is not the paper that was asked for.

    Raised only on positive evidence -- the front pages name a different document, or the
    file is a fragment of the record -- never because the file could not be checked. Like
    NotAPDFError it is not retried: the server will send the same document again. The
    download stage moves on to the next candidate. See paper_downloader.download.identity.
    """

    def __init__(self, message: str, *, verdict: dict | None = None, final_url: str = "") -> None:
        super().__init__(message)
        self.verdict = verdict or {}
        self.final_url = final_url


class HostBlockedError(DownloadError):
    """The host turned this client away without answering about the paper.

    Kept apart from HTTPStatusError because the two mean opposite things to a caller
    deciding whether to try again: a 403 on an article is a fact about the article, whereas
    this is a fact about us.

    Three different facts, and the flags say which -- because they have three different
    remedies and a caller that flattens them will retry the hopeless and give up on the
    recoverable:

    * ``denied``: the host is on the configured deny list and was never asked. Configuration.
    * ``challenge``: a bot wall answered. No address, delay or User-Agent changes this; the
      route is a publisher API, a cached copy elsewhere, or a person with a browser.
    * neither: this address was refused, in a rate- or IP-shaped way. Worth another attempt
      once the cool-off passes, or from a different network.
    """

    def __init__(
        self, message: str, *, host: str = "", denied: bool = False, challenge: bool = False,
    ) -> None:
        super().__init__(message)
        self.host = host
        #: True when the host was never asked because it is on the configured deny list,
        #: as opposed to having actually refused a request. Callers storing the detail
        #: must not describe the first as the second.
        self.denied = denied
        #: True when a bot-management product answered instead of the host. See host_gate.
        self.challenge = challenge


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