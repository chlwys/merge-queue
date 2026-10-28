"""Deterministic local merge queue for Git repositories.

`merge_queue.core` is the queue itself (pending CLI waits lazily load watchfiles); `merge_queue.mcp_server` is the
optional MCP completion adapter. See the documentation in docs/.
"""
__version__ = "0.1.2"
