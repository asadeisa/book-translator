"""Fixed URI strings, kept in one place.

WEB_LINK is a regular expression that finds web links inside the book's text,
so the checks can make sure every link survives translation. The NS_* values
are XML namespace identifiers that the EPUB 3 and XHTML specs require inside
the output files. None of these are fetched: the scripts make no network
requests.
"""

WEB_LINK = r"(?:https?|ftp)://[^\s<>\"')\]]+[^\s<>\"')\].,;:!?]"

NS_XHTML = "http://www.w3.org/1999/xhtml"
NS_OPS = "http://www.idpf.org/2007/ops"
NS_OPF = "http://www.idpf.org/2007/opf"
NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"
NS_DC = "http://purl.org/dc/elements/1.1/"
