"""Errors shared by the file loaders."""


class SpecError(ValueError):
    """A flow or config file is invalid. The message names the file and the offending field."""
