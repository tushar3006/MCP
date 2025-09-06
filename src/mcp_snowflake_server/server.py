import importlib.metadata
import json
import logging
import os
from functools import wraps
from typing import Any, Callable

import mcp.server.stdio
import mcp.types as types
import yaml
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from pydantic import AnyUrl, BaseModel

from .db_client import SnowflakeDB
from .write_detector import SQLWriteDetector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("mcp_snowflake_server")


def data_to_yaml(data: Any) -> str:
    return yaml.dump(data, indent=2, sort_keys=False)

# Custom serializer that checks for 'date' type
def data_json_serializer(obj):
    from datetime import date, datetime
    if isinstance(obj, date) or isinstance(obj, datetime):
        return obj.isoformat()
    else:
        return obj


def handle_tool_errors(func: Callable) -> Callable:
    """Decorator to standardize tool error handling"""

    @wraps(func)
    async def wrapper(*args, **kwargs) -> list[types.TextContent]:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {str(e)}")
            return [types.TextContent(type="text", text=f"Error: {str(e)}")]

    return wrapper


class Tool(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[
        [str, dict[str, Any] | None],
        list[types.TextContent | types.ImageContent | types.EmbeddedResource],
    ]
    tags: list[str] = []


# Tool handlers
async def handle_list_databases(arguments, db, *_, exclusion_config=None):
    query = "SELECT DATABASE_NAME FROM INFORMATION_SCHEMA.DATABASES"
    data, data_id = await db.execute_query(query)

    # Filter out excluded databases
    if exclusion_config and "databases" in exclusion_config and exclusion_config["databases"]:
        filtered_data = []
        for item in data:
            db_name = item.get("DATABASE_NAME", "")
            exclude = False
            for pattern in exclusion_config["databases"]:
                if pattern.lower() in db_name.lower():
                    exclude = True
                    break
            if not exclude:
                filtered_data.append(item)
        data = filtered_data

    output = {
        "type": "data",
        "data_id": data_id,
        "data": data,
    }
    yaml_output = data_to_yaml(output)
    json_output = json.dumps(output)
    return [
        types.TextContent(type="text", text=yaml_output),
        types.EmbeddedResource(
            type="resource",
            resource=types.TextResourceContents(uri=f"data://{data_id}", text=json_output, mimeType="application/json"),
        ),
    ]


async def handle_list_schemas(arguments, db, *_, exclusion_config=None):
    if not arguments or "database" not in arguments:
        raise ValueError("Missing required 'database' parameter")

    database = arguments["database"]
    query = f"SELECT SCHEMA_NAME FROM {database.upper()}.INFORMATION_SCHEMA.SCHEMATA"
    data, data_id = await db.execute_query(query)

    # Filter out excluded schemas
    if exclusion_config and "schemas" in exclusion_config and exclusion_config["schemas"]:
        filtered_data = []
        for item in data:
            schema_name = item.get("SCHEMA_NAME", "")
            exclude = False
            for pattern in exclusion_config["schemas"]:
                if pattern.lower() in schema_name.lower():
                    exclude = True
                    break
            if not exclude:
                filtered_data.append(item)
        data = filtered_data

    output = {
        "type": "data",
        "data_id": data_id,
        "database": database,
        "data": data,
    }
    yaml_output = data_to_yaml(output)
    json_output = json.dumps(output)
    return [
        types.TextContent(type="text", text=yaml_output),
        types.EmbeddedResource(
            type="resource",
            resource=types.TextResourceContents(uri=f"data://{data_id}", text=json_output, mimeType="application/json"),
        ),
    ]


async def handle_list_tables(arguments, db, *_, exclusion_config=None):
    if not arguments or "database" not in arguments or "schema" not in arguments:
        raise ValueError("Missing required 'database' and 'schema' parameters")

    database = arguments["database"]
    schema = arguments["schema"]

    query = f"""
        SELECT table_catalog, table_schema, table_name, comment 
        FROM {database}.information_schema.tables 
        WHERE table_schema = '{schema.upper()}'
    """
    data, data_id = await db.execute_query(query)

    # Filter out excluded tables
    if exclusion_config and "tables" in exclusion_config and exclusion_config["tables"]:
        filtered_data = []
        for item in data:
            table_name = item.get("TABLE_NAME", "")
            exclude = False
            for pattern in exclusion_config["tables"]:
                if pattern.lower() in table_name.lower():
                    exclude = True
                    break
            if not exclude:
                filtered_data.append(item)
        data = filtered_data

    output = {
        "type": "data",
        "data_id": data_id,
        "database": database,
        "schema": schema,
        "data": data,
    }
    yaml_output = data_to_yaml(output)
    json_output = json.dumps(output)
    return [
        types.TextContent(type="text", text=yaml_output),
        types.EmbeddedResource(
            type="resource",
            resource=types.TextResourceContents(uri=f"data://{data_id}", text=json_output, mimeType="application/json"),
        ),
    ]


async def handle_describe_table(arguments, db, *_):
    if not arguments or "table_name" not in arguments:
        raise ValueError("Missing table_name argument")

    table_spec = arguments["table_name"]
    split_identifier = table_spec.split(".")

    # Parse the fully qualified table name
    if len(split_identifier) < 3:
        raise ValueError("Table name must be fully qualified as 'database.schema.table'")

    database_name = split_identifier[0].upper()
    schema_name = split_identifier[1].upper()
    table_name = split_identifier[2].upper()

    query = f"""
        SELECT column_name, column_default, is_nullable, data_type, comment 
        FROM {database_name}.information_schema.columns 
        WHERE table_schema = '{schema_name}' AND table_name = '{table_name}'
    """
    data, data_id = await db.execute_query(query)

    output = {
        "type": "data",
        "data_id": data_id,
        "database": database_name,
        "schema": schema_name,
        "table": table_name,
        "data": data,
    }
    yaml_output = data_to_yaml(output)
    json_output = json.dumps(output)
    return [
        types.TextContent(type="text", text=yaml_output),
        types.EmbeddedResource(
            type="resource",
            resource=types.TextResourceContents(uri=f"data://{data_id}", text=json_output, mimeType="application/json"),
        ),
    ]


async def handle_read_query(arguments, db, write_detector, *_):
    if not arguments or "query" not in arguments:
        raise ValueError("Missing query argument")

    if write_detector.analyze_query(arguments["query"])["contains_write"]:
        raise ValueError("Calls to read_query should not contain write operations")

    data, data_id = await db.execute_query(arguments["query"])

    output = {
        "type": "data",
        "data_id": data_id,
        "data": data,
    }
    yaml_output = data_to_yaml(output)
    json_output = json.dumps(output, default=data_json_serializer)
    return [
        types.TextContent(type="text", text=yaml_output),
        types.EmbeddedResource(
            type="resource",
            resource=types.TextResourceContents(uri=f"data://{data_id}", text=json_output, mimeType="application/json"),
        ),
    ]


async def handle_append_insight(arguments, db, _, __, server):
    if not arguments or "insight" not in arguments:
        raise ValueError("Missing insight argument")

    db.add_insight(arguments["insight"])
    await server.request_context.session.send_resource_updated(AnyUrl("memo://insights"))
    return [types.TextContent(type="text", text="Insight added to memo")]


async def handle_write_query(arguments, db, _, allow_write, __):
    if not allow_write:
        raise ValueError("Write operations are not allowed for this data connection")
    if arguments["query"].strip().upper().startswith("SELECT"):
        raise ValueError("SELECT queries are not allowed for write_query")

    results, data_id = await db.execute_query(arguments["query"])
    return [types.TextContent(type="text", text=str(results))]


async def handle_create_table(arguments, db, _, allow_write, __):
    if not allow_write:
        raise ValueError("Write operations are not allowed for this data connection")
    if not arguments["query"].strip().upper().startswith("CREATE TABLE"):
        raise ValueError("Only CREATE TABLE statements are allowed")

    results, data_id = await db.execute_query(arguments["query"])
    return [types.TextContent(type="text", text=f"Table created successfully. data_id = {data_id}")]


@handle_tool_errors
async def handle_get_event_media(arguments, db, *_):
    """Get event media data (videos/images) for violations"""
    if not arguments:
        raise ValueError("Missing arguments for event media request")
    
    site_id = arguments.get("site_id")
    violation_ids = arguments.get("violation_ids", [])
    
    if not site_id:
        raise ValueError("site_id is required for security filtering")
    
    if not violation_ids or len(violation_ids) == 0:
        raise ValueError("violation_ids are required")
    
    # Convert violation_ids to strings and validate
    violation_ids = [str(vid) for vid in violation_ids]
    
    # Get database connection details to determine region
    connection_args = db.connection_args or {}
    database = connection_args.get("database", "").upper()
    
    # Map database to region
    region_map = {
        "PROTEX_DB_US": "us",
        "PROTEX_DB_EU": "eu", 
        "PROTEX_DB_APAC": "apac",
        "PROTEX_DB_CA": "ca",
        "PROTEX_DB_TESLA": "us"
    }
    
    region = region_map.get(database, "eu")  # Default to EU
    
    # Build the query to get event UUIDs
    violation_ids_str = "', '".join(violation_ids)
    query = f"""
        SELECT VIOLATION_ID, DEVICE_EVENT_UUID, SITE_ID, CAMERA_ID, RULE_ID, VIOLATION_DATE_WITH_TIMEZONE
        FROM {database}.PUBLIC.mart_shifts 
        WHERE SITE_ID = {site_id} 
        AND VIOLATION_ID IN ('{violation_ids_str}')
        AND DEVICE_EVENT_UUID IS NOT NULL 
        ORDER BY VIOLATION_DATE_WITH_TIMEZONE DESC
    """
    
    try:
        data, data_id = await db.execute_query(query)
        
        if not data:
            return [types.TextContent(
                type="text", 
                text=f"No event media found for site {site_id} with violation IDs: {', '.join(violation_ids)}"
            )]
        
        # Create clean, structured response for frontend
        response_text = f"Found {len(data)} event video{'s' if len(data) != 1 else ''} for Site {site_id}:\n\n"
        
        # Create event_media structured response
        event_media_data = {
            "type": "event_media",
            "data_id": data_id,
            "site_id": site_id,
            "region": region,
            "data": []
        }
        
        # Add each event media
        for row in data:
            event_media_data["data"].append({
                "REGION": region,
                "DEVICE_EVENT_UUID": row["DEVICE_EVENT_UUID"]
            })
        
        # Add structured JSON and embedded resource
        yaml_output = data_to_yaml(event_media_data)
        json_output = json.dumps(event_media_data, default=data_json_serializer)
        
        return [
            types.TextContent(type="text", text=f"{response_text}{yaml_output}"),
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri=f"event_media://{data_id}", 
                    text=json_output, 
                    mimeType="application/json"
                ),
            ),
        ]
        
    except Exception as e:
        logger.error(f"Error fetching event media: {str(e)}")
        raise ValueError(f"Failed to fetch event media: {str(e)}")


async def prefetch_tables(db: SnowflakeDB, credentials: dict) -> dict:
    """Prefetch table and column information"""
    try:
        logger.info("Prefetching table descriptions")
        table_results, data_id = await db.execute_query(
            f"""SELECT table_name, comment 
                FROM {credentials['database']}.information_schema.tables 
                WHERE table_schema = '{credentials['schema'].upper()}'"""
        )

        column_results, data_id = await db.execute_query(
            f"""SELECT table_name, column_name, data_type, comment 
                FROM {credentials['database']}.information_schema.columns 
                WHERE table_schema = '{credentials['schema'].upper()}'"""
        )

        tables_brief = {}
        for row in table_results:
            tables_brief[row["TABLE_NAME"]] = {**row, "COLUMNS": {}}

        for row in column_results:
            row_without_table_name = row.copy()
            del row_without_table_name["TABLE_NAME"]
            tables_brief[row["TABLE_NAME"]]["COLUMNS"][row["COLUMN_NAME"]] = row_without_table_name

        return tables_brief

    except Exception as e:
        logger.error(f"Error prefetching table descriptions: {e}")
        return f"Error prefetching table descriptions: {e}"


async def main(
    allow_write: bool = False,
    connection_args: dict = None,
    log_dir: str = None,
    prefetch: bool = False,
    log_level: str = "INFO",
    exclude_tools: list[str] = [],
    config_file: str = "runtime_config.json",
    exclude_patterns: dict = None,
):
    # Setup logging
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        logger.handlers.append(logging.FileHandler(os.path.join(log_dir, "mcp_snowflake_server.log")))
    if log_level:
        logger.setLevel(log_level)

    logger.info("Starting Snowflake MCP Server")
    logger.info("Allow write operations: %s", allow_write)
    logger.info("Prefetch table descriptions: %s", prefetch)
    logger.info("Excluded tools: %s", exclude_tools)

    # Load configuration from file if provided
    config = {}
    #
    if config_file:
        try:
            with open(config_file, "r") as f:
                config = json.load(f)
                logger.info(f"Loaded configuration from {config_file}")
        except Exception as e:
            logger.error(f"Error loading configuration file: {e}")

    # Merge exclude_patterns from parameters with config file
    exclusion_config = config.get("exclude_patterns", {})
    if exclude_patterns:
        # Merge patterns from parameters with those from config file
        for key, patterns in exclude_patterns.items():
            if key in exclusion_config:
                exclusion_config[key].extend(patterns)
            else:
                exclusion_config[key] = patterns

    # Set default patterns if none are specified
    if not exclusion_config:
        exclusion_config = {"databases": [], "schemas": [], "tables": []}

    # Ensure all keys exist in the exclusion config
    for key in ["databases", "schemas", "tables"]:
        if key not in exclusion_config:
            exclusion_config[key] = []

    logger.info(f"Exclusion patterns: {exclusion_config}")

    db = SnowflakeDB(connection_args)
    db.start_init_connection()
    server = Server("snowflake-manager")
    write_detector = SQLWriteDetector()

    tables_info = (await prefetch_tables(db, connection_args)) if prefetch else {}
    tables_brief = data_to_yaml(tables_info) if prefetch else ""

    all_tools = [
        Tool(
            name="list_databases",
            description="List all available databases in Snowflake",
            input_schema={
                "type": "object",
                "properties": {},
            },
            handler=handle_list_databases,
        ),
        Tool(
            name="list_schemas",
            description="List all schemas in a database",
            input_schema={
                "type": "object",
                "properties": {
                    "database": {
                        "type": "string",
                        "description": "Database name to list schemas from",
                    },
                },
                "required": ["database"],
            },
            handler=handle_list_schemas,
        ),
        Tool(
            name="list_tables",
            description="List all tables in a specific database and schema",
            input_schema={
                "type": "object",
                "properties": {
                    "database": {"type": "string", "description": "Database name"},
                    "schema": {"type": "string", "description": "Schema name"},
                },
                "required": ["database", "schema"],
            },
            handler=handle_list_tables,
        ),
        Tool(
            name="describe_table",
            description="Get the schema information for a specific table",
            input_schema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Fully qualified table name in the format 'database.schema.table'",
                    },
                },
                "required": ["table_name"],
            },
            handler=handle_describe_table,
        ),
        Tool(
            name="read_query",
            description="Execute a SELECT query.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "SELECT SQL query to execute"}},
                "required": ["query"],
            },
            handler=handle_read_query,
        ),
        Tool(
            name="append_insight",
            description="Add a data insight to the memo",
            input_schema={
                "type": "object",
                "properties": {
                    "insight": {
                        "type": "string",
                        "description": "Data insight discovered from analysis",
                    }
                },
                "required": ["insight"],
            },
            handler=handle_append_insight,
            tags=["resource_based"],
        ),
        Tool(
            name="write_query",
            description="Execute an INSERT, UPDATE, or DELETE query on the Snowflake database",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "SQL query to execute"}},
                "required": ["query"],
            },
            handler=handle_write_query,
            tags=["write"],
        ),
        Tool(
            name="create_table",
            description="Create a new table in the Snowflake database",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "CREATE TABLE SQL statement"}},
                "required": ["query"],
            },
            handler=handle_create_table,
            tags=["write"],
        ),
        Tool(
            name="get_event_media",
            description="Get event media (videos/images) for specific violations at a site. Returns structured JSON that the frontend automatically displays as interactive media players.",
            input_schema={
                "type": "object",
                "properties": {
                    "site_id": {
                        "type": "integer",
                        "description": "Site ID for security filtering (REQUIRED)"
                    },
                    "violation_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of violation IDs to get media for (REQUIRED)"
                    }
                },
                "required": ["site_id", "violation_ids"],
            },
            handler=handle_get_event_media,
            tags=["media", "video", "auto_call"],
        ),
    ]

    exclude_tags = []
    if not allow_write:
        exclude_tags.append("write")
    allowed_tools = [
        tool for tool in all_tools if tool.name not in exclude_tools and not any(tag in exclude_tags for tag in tool.tags)
    ]

    logger.info("Allowed tools: %s", [tool.name for tool in allowed_tools])

    # Register handlers
    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        resources = [
            types.Resource(
                uri=AnyUrl("memo://insights"),
                name="Data Insights Memo",
                description="A living document of discovered data insights",
                mimeType="text/plain",
            )
        ]
        table_brief_resources = [
            types.Resource(
                uri=AnyUrl(f"context://table/{table_name}"),
                name=f"{table_name} table",
                description=f"Description of the {table_name} table",
                mimeType="text/plain",
            )
            for table_name in tables_info.keys()
        ]
        resources += table_brief_resources
        return resources

    @server.read_resource()
    async def handle_read_resource(uri: AnyUrl) -> str:
        if str(uri) == "memo://insights":
            return db.get_memo()
        elif str(uri).startswith("context://table"):
            table_name = str(uri).split("/")[-1]
            if table_name in tables_info:
                return data_to_yaml(tables_info[table_name])
            else:
                raise ValueError(f"Unknown table: {table_name}")
        else:
            raise ValueError(f"Unknown resource: {uri}")

    @server.list_prompts()
    async def handle_list_prompts() -> list[types.Prompt]:
        return []

    @server.get_prompt()
    async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
        raise ValueError(f"Unknown prompt: {name}")

    @server.call_tool()
    @handle_tool_errors
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        if name in exclude_tools:
            return [types.TextContent(type="text", text=f"Tool {name} is excluded from this data connection")]

        handler = next((tool.handler for tool in allowed_tools if tool.name == name), None)
        if not handler:
            raise ValueError(f"Unknown tool: {name}")

        # Pass exclusion_config to the handler if it's a listing function
        if name in ["list_databases", "list_schemas", "list_tables"]:
            return await handler(
                arguments,
                db,
                write_detector,
                allow_write,
                server,
                exclusion_config=exclusion_config,
            )
        else:
            return await handler(arguments, db, write_detector, allow_write, server)

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        logger.info("Listing tools")
        logger.error(f"Allowed tools: {allowed_tools}")
        tools = [
            types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.input_schema,
            )
            for tool in allowed_tools
        ]
        return tools

    # Start server
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        logger.info("Server running with stdio transport")
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="snowflake",
                server_version=importlib.metadata.version("mcp_snowflake_server"),
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )
