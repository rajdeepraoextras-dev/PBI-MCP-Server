"""pbi-service: optional MCP server for the Power BI / Fabric service.

Cloud counterpart of the local-file servers: publishes a .pbip project as
SemanticModel + Report items, pulls item definitions back into PBIP folders,
refreshes datasets, drives deployment pipelines and exports reports. The
model and report servers never import this package.
"""
