"""
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
"""
MCP Client maintains Multi-MCP-Servers

Supports multiple transport mechanisms:
- stdio: For local server processes
- SSE: For server-sent events
- StreamableHTTP: For HTTP/HTTPS connections with optional streaming
"""
import os
import logging
import asyncio
from typing import Optional, Dict
from contextlib import AsyncExitStack
from datetime import timedelta
from pydantic import ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client, get_default_environment
from mcp.types import Resource, Tool, TextContent, ImageContent, EmbeddedResource,CallToolResult,NotificationParams
from mcp.shared.exceptions import McpError
from dotenv import load_dotenv
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

load_dotenv()  # load environment variables from .env

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
)
logger = logging.getLogger(__name__)
delimiter = "___"
tool_name_mapping = {}
tool_name_mapping_r = {}
class MCPClient:
    """Manage MCP sessions.

    Support features:
    - MCP multi-server
    - get tool config from server
    - call tool and get result from server
    """

    def __init__(self, name, access_key_id='', secret_access_key='', region='us-east-1'):
        self.env = {
            'AWS_ACCESS_KEY_ID': access_key_id or os.environ.get('AWS_ACCESS_KEY_ID'),
            'AWS_SECRET_ACCESS_KEY': secret_access_key or os.environ.get('AWS_SECRET_ACCESS_KEY'),
            'AWS_REGION': region or os.environ.get('AWS_REGION'),
        }
        self.name = name
        # self.sessions: Dict[str, Optional[ClientSession]] = {}
        self.session = None
        self.exit_stack = AsyncExitStack()
        self._is_http_connection = False

    @staticmethod
    def normalize_tool_name( tool_name):
        return tool_name.replace('-', '_').replace('/', '_').replace(':', '_')
    
    @staticmethod
    def get_tool_name4llm( server_id, tool_name, norm=True, ns_delimiter=delimiter):
        """Convert MCP server tool name to llm tool call"""
        global tool_name_mapping, tool_name_mapping_r
        # prepend server prefix namespace to support multi-mcp-server
        tool_key = server_id + ns_delimiter + tool_name
        tool_name4llm = tool_key if not norm else MCPClient.normalize_tool_name(tool_key)
        tool_name_mapping[tool_key] = tool_name4llm
        tool_name_mapping_r[tool_name4llm] = tool_key
        return tool_name4llm
    
    @staticmethod
    def get_tool_name4mcp( tool_name4llm, ns_delimiter=delimiter):
        """Convert llm tool call name to MCP server original name"""
        global  tool_name_mapping_r
        server_id, tool_name = "", ""
        tool_name4mcp = tool_name_mapping_r.get(tool_name4llm, "")
        if len(tool_name4mcp.split(ns_delimiter)) == 2:
            server_id, tool_name = tool_name4mcp.split(ns_delimiter)
        return server_id, tool_name

    async def disconnect_to_server(self):
        logger.info(f"\nDisconnecting to server [{self.name}]")
        await self.cleanup()

    async def handle_resource_change(self, params: NotificationParams):
        print(f"Resource change type: {params['changeType']}")
        print(f"Affected URIs: {params['resourceURIs']}")
    async def connect_via_http(self, url, headers=None, timeout=30, sse_read_timeout=300):
        """Connect to an MCP server using StreamableHTTP transport
        
        Args:
            url: The endpoint URL for the MCP server
            headers: Optional headers to include in requests
            timeout: HTTP timeout for regular operations (in seconds)
            sse_read_timeout: Timeout for SSE read operations (in seconds)
            
        Returns:
            bool: True if connection was successful
        """
        try:
            # Ensure headers are serializable
            safe_headers = {}
            if headers:
                for k, v in headers.items():
                    if isinstance(v, (str, int, float, bool, type(None))):
                        safe_headers[k] = v
                    else:
                        safe_headers[k] = str(v)
            
            transport = streamablehttp_client(
                url=url,
                headers=safe_headers,
                timeout=timedelta(seconds=timeout),
                sse_read_timeout=timedelta(seconds=sse_read_timeout),
                terminate_on_close=False  # We don't need session management
            )
            
            logger.info(f"\nConnecting to HTTP server at {url}")
            
            # Handle the tuple unpacking based on what streamablehttp_client returns
            transport_result = await self.exit_stack.enter_async_context(transport)
            if len(transport_result) == 3:
                _stdio, _write, _ = transport_result
            else:
                _stdio, _write = transport_result
                
            self.session = await self.exit_stack.enter_async_context(ClientSession(_stdio, _write))
            await self.session.initialize()
            logger.info(f"\n{self.name} HTTP session initialized")
            
            # Mark this as an HTTP connection
            self._is_http_connection = True
            
            await self.list_mcp_server()
            return True
        except Exception as e:
            logger.error(f"\n{self.name} HTTP session initialization failed: {e}")
            raise ValueError(f"Failed to connect to HTTP server: {e}")
    
    
    async def connect_to_server(self, server_script_path: str = "", server_script_args: list = [], 
            server_script_envs: Dict = {}, command: str = "", server_url: str = "",
            http_headers: Dict = None, http_timeout: int = 30, http_sse_timeout: int = 300):
        """Connect to an MCP server
        
        Args:
            server_script_path: Path to server script
            server_script_args: Arguments for server script
            server_script_envs: Environment variables for server script
            command: Command to run server
            server_url: URL for HTTP/SSE server connection
            http_headers: Headers for HTTP connection (only used with server_url)
            http_timeout: Timeout for HTTP operations in seconds (only used with server_url)
            http_sse_timeout: Timeout for SSE operations in seconds (only used with server_url)
            
        Returns:
            bool: True if connection was successful
        """
        # If server_url is provided and it's an HTTP URL, use StreamableHTTP transport
        if server_url and (server_url.startswith('http://') or server_url.startswith('https://')):
            return await self.connect_via_http(
                url=server_url,
                headers=http_headers,
                timeout=http_timeout,
                sse_read_timeout=http_sse_timeout
            )
            
        # Otherwise, use existing stdio or SSE transport logic
        if server_script_path:
            # run via script
            is_python = server_script_path.endswith('.py')
            is_js = server_script_path.endswith('.js')
            is_uvx = server_script_path.startswith('uvx:')
            is_np = server_script_path.startswith('npx:')
            is_docker = server_script_path.startswith('docker:')
            is_uv = server_script_path.startswith('uv:')

            if not (is_python or is_js or is_uv or is_np or is_docker or is_uvx):
                raise ValueError("Server script must be a .py or .js file or package")
            if is_uv or is_np or is_uvx:
                server_script_path = server_script_path[server_script_path.index(':')+1:]

            server_script_args = [server_script_path] + server_script_args
    
            if is_python:
                command = "python"
            elif is_uv:
                command = "uv"
            elif is_uvx:
                command = "uvx"
            elif is_np:
                command = "npx"
                server_script_args = ["-y"] + server_script_args
            elif is_js:
                command = "node"
            elif is_docker:
                command = "docker"

        env = get_default_environment()
        if self.env['AWS_ACCESS_KEY_ID'] and self.env['AWS_SECRET_ACCESS_KEY']:
            env['AWS_ACCESS_KEY_ID'] =  self.env['AWS_ACCESS_KEY_ID']
            env['AWS_SECRET_ACCESS_KEY'] = self.env['AWS_SECRET_ACCESS_KEY']
            env['AWS_REGION'] = self.env['AWS_REGION']
        env.update(server_script_envs)
        
        # Mark this as not an HTTP connection
        self._is_http_connection = False
        
        try: 
            if server_url:
                # Mark this as an HTTP connection
                self._is_http_connection = True
                transport = sse_client(server_url)
                logger.info(f"\nAdding HTTP server with URL: {server_url}")
            else:
                # This is a stdio connection
                self._is_http_connection = False
                transport = stdio_client(StdioServerParameters(
                    command=command, args=server_script_args, env=env
                ))
                logger.info(f"\nAdding server with command: {command} and args: {server_script_args}")
        except Exception as e:
            logger.error(f"\n{e}")
            raise ValueError(f"Invalid server script or command. {e}")
        try:
            _stdio, _write = await self.exit_stack.enter_async_context(transport)
            self.session = await self.exit_stack.enter_async_context(ClientSession(_stdio, _write))
            await self.session.initialize()
            logger.info(f"\n{self.name} session initialize done")
        except Exception as e:
            logger.error(f"\n{self.name} session initialize failed: {e}")
            raise ValueError(f"Invalid server script or command. {e}")   
        await self.list_mcp_server()
        return True
        
    async def list_mcp_server(self):
        try:
            resource = await self.session.list_resources()
            logger.info(f"\n{self.name} list_resources:{resource}")
        except McpError as e:
            logger.info(f"\n{self.name} list_resources:{str(e)}")
        # List available tools
        response = await self.session.list_tools()
        tools = response.tools
        logger.info(f"\nConnected to server [{self.name}] with tools: " + str([tool for tool in tools]))
        
    def is_http_connection(self):
        """Check if the current connection is using StreamableHTTP transport"""
        return hasattr(self, '_is_http_connection') and self._is_http_connection
        
    async def get_tool_config(self, model_provider='bedrock', server_id : str = ''):
        """Get llm's tool usage config via MCP server"""
        # list tools via mcp server
        try:
            response = await self.session.list_tools()
            if not response:
                logger.error('list_tools returns empty')
                raise ValueError('list_tools returns empty')
        except Exception as e:
            logger.error(f'{e}')
            return None

        # for bedrock tool config
        tool_config = {"tools": []}
        tool_config["tools"].extend([{
            "toolSpec":{
                # mcp tool's original name to llm tool name (with server id namespace)
                "name": MCPClient.get_tool_name4llm(server_id, tool.name, norm=True),
                "description": tool.description, 
                "inputSchema": {"json": tool.inputSchema}
            }
        } for tool in response.tools])

        return tool_config

    async def call_tool(self, tool_name, tool_args):
        """Call tool via MCP server"""
        try:
            result = await self.session.call_tool(tool_name, tool_args)
            return result
        except ValidationError as e:
            # Extract the actual tool result from the validation error
            raw_data = e.errors() if hasattr(e, 'errors') else None
            logger.info(f"raw_data:{raw_data}")
            if raw_data and len(raw_data) > 0:
                tool_result = raw_data[0]['input']
                
                return CallToolResult.model_validate(tool_result)
            # Re-raise the exception if the result cannot be extracted
            raise

    async def cleanup(self):
        """Clean up resources"""
        try:
            await self.exit_stack.aclose()
        except RuntimeError as e:
            # Handle the case where exit_stack is being closed in a different task
            if "Attempted to exit cancel scope in a different task" in str(e):
                # Create a new exit stack for future use
                self.exit_stack = AsyncExitStack()
                # logger.warning(f"Handled cross-task exit_stack closure for {self.name}")
            else:
                # Re-raise if it's a different error
                raise
