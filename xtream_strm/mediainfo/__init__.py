"""Core, source-independent technical media information.

Producers (the ffprobe adapter today; MediaInfo, provider data or manual edits later)
fill :class:`~xtream_strm.mediainfo.models.MediaInfo`. The NFO parser/writer in
:mod:`xtream_strm.nfo` is the only code that maps this model to and from XML.
"""
