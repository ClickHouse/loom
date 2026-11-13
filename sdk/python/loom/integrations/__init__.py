"""Adapters that present loom's memory and documents through agent frameworks'
own interfaces. An adapter that subclasses the framework's classes imports it
when the module loads and needs the SDK's extra for it
(``pip install 'clickhouse-loom[langchain]'``); the others import it lazily."""
