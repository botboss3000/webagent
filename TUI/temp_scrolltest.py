import asyncio, os
os.environ.setdefault("WEBAGENT_PROJECT", os.getcwd())
from textual.widgets import Static
from textual.events import MouseScrollDown
from webagent.app import ServerManagerApp  # guess name
