"""chatgpt_archiver — export ChatGPT conversations to clean Markdown.

Reads your existing Chrome session (no login prompts, no DOM scraping) and
calls the same backend API the ChatGPT web app itself uses, so exports get
the original markdown source instead of a lossy copy-paste of rendered HTML.
"""

__version__ = "0.1.0"
