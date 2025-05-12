"""
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
"""
FastAPI server for Bedrock Chat with MCP support

This server supports multiple transport mechanisms for MCP clients:
- stdio: For local server processes
- SSE: For server-sent events
- StreamableHTTP: For HTTP/HTTPS connections with optional streaming
"""
import os
import sys
import json
import time
import argparse
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Literal, AsyncGenerator
import uuid
import threading
from contextlib import asynccontextmanager
import os
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Depends, BackgroundTasks, Security
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security.api_key import APIKeyHeader
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Security
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from fastapi.exceptions import RequestValidationError
from mcp_client import MCPClient
from chat_client_stream import ChatClientStream
from mcp.shared.exceptions import McpError

# Global model and server configuration
load_dotenv()  # load env vars from .env
llm_model_list = {}
shared_mcp_server_list = {}  # Shared MCP server description information
global_mcp_server_configs = {}  # Global MCP server configuration server_id -> config
user_mcp_server_configs = {}  # User-specific MCP server configuration user_id -> {server_id: config}
MAX_TURNS = int(os.environ.get("MAX_TURNS",200))
INACTIVE_TIME = int(os.environ.get("INACTIVE_TIME",60*24))  #mins


API_KEY = os.environ.get("API_KEY")
security = HTTPBearer()

logger = logging.getLogger(__name__)


# 用户会话管理
class UserSession:
    def __init__(self, user_id):
        self.user_id = user_id
        if os.path.exists("conf/credentials.csv"):
            self.chat_client = ChatClientStream(credential_file="conf/credentials.csv")
        else:
            self.chat_client = ChatClientStream()
        self.mcp_clients = {}  # User-specific MCP clients
        self.last_active = datetime.now()
        self.session_id = str(uuid.uuid4())
        self.lock = asyncio.Lock()  # For synchronizing operations within the session

    async def cleanup(self):
        """Clean up user session resources"""
        cleanup_tasks = []
        for client_id, client in self.mcp_clients.items():
            cleanup_tasks.append(client.cleanup())
        
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks)
            logger.info(f"User {self.user_id}'s {len(cleanup_tasks)} MCP clients have been cleaned up")

# User session storage
user_sessions = {}
# Session lock to prevent race conditions in session creation and access
session_lock = threading.RLock()

async def get_api_key(auth: HTTPAuthorizationCredentials = Security(security)):
    if auth.credentials == API_KEY:
        return auth.credentials
    raise HTTPException(status_code=403, detail="Could not validate credentials")

# Save global MCP server configuration
def save_global_server_config( server_id: str, config: dict):
    """Save global MCP server configuration"""
    global global_mcp_server_configs
    global_mcp_server_configs[server_id] = config
    # In a real application, configuration should be persisted to database or file system
    logger.info(f"Saved Global server configuration {server_id}")
    
# Save user MCP server configuration
def save_user_server_config(user_id: str, server_id: str, config: dict):
    """Save user's MCP server configuration"""
    global user_mcp_server_configs
    with session_lock:
        if user_id not in user_mcp_server_configs:
            user_mcp_server_configs[user_id] = {}
        
        user_mcp_server_configs[user_id][server_id] = config
        # In a real application, configuration should be persisted to database or file system
        logger.info(f"Saved server configuration {server_id} for user {user_id}")

# Get user MCP server configuration
def get_user_server_configs(user_id: str) -> dict:
    """Get all MCP server configurations for specified user"""
    return user_mcp_server_configs.get(user_id, {})

# Get global server configuration
def get_global_server_configs() -> dict:
    """Get all global MCP server configurations"""
    return global_mcp_server_configs

async def load_user_mcp_configs():
    """Load user MCP server configurations"""
    # Load from file or database
    try:
        config_file = os.environ.get('USER_MCP_CONFIG_FILE', 'conf/user_mcp_configs.json')
        if os.path.exists(config_file):
            with session_lock:
                with open(config_file, 'r') as f:
                    configs = json.load(f)
                    global user_mcp_server_configs
                    user_mcp_server_configs = configs
                    logger.info(f"Loaded MCP server configurations for {len(configs)} users")
    except Exception as e:
        logger.error(f"Failed to load user MCP configurations: {e}")

async def save_user_mcp_configs():
    global user_mcp_server_configs
    # user_mcp_server_configs[user_id] = server_configs
    """Save user MCP server configurations"""
    # Save to file or database
    try:
        config_file = os.environ.get('USER_MCP_CONFIG_FILE', 'conf/user_mcp_configs.json')
        #add thread lock
        with session_lock:
            with open(config_file, 'w') as f:
                json.dump(user_mcp_server_configs, f, indent=2)
                logger.info(f"Saved MCP server configurations for {len(user_mcp_server_configs)} users")
    except Exception as e:
        logger.error(f"Failed to save user MCP configurations: {e}")
        
async def initialize_user_servers(session: UserSession):
    """Initialize user-specific MCP servers"""
    user_id = session.user_id
    
    server_configs = get_user_server_configs(user_id)
    
    global_server_configs = get_global_server_configs()
    #Merge global and user servers
    server_configs = {**server_configs,**global_server_configs}
    
    logger.info(f"server_configs:{server_configs}")
    # Initialize server connections
    for server_id, config in server_configs.items():
        if server_id in session.mcp_clients:  # Skip existing servers
            continue
            
        try:
            # Create and connect to MCP server
            mcp_client = MCPClient(name=f"{session.user_id}_{server_id}")
            
            # Check if this is an HTTP connection
            if "server_url" in config and config["server_url"]:
                await mcp_client.connect_to_server(
                    server_url=config["server_url"],
                    http_headers=config.get("http_headers", {}),
                    http_timeout=config.get("http_timeout", 30),
                    http_sse_timeout=config.get("http_sse_timeout", 300)
                )
            else:
                # Standard connection
                await mcp_client.connect_to_server(
                    command=config["command"],
                    server_script_args=config.get("args", []),
                    server_script_envs=config.get("env", {})
                )
            
            # Add to user's client list
            session.mcp_clients[server_id] = mcp_client
            
            save_user_server_config(user_id, server_id, config)

            await save_user_mcp_configs()
            logger.info(f"User Id {session.user_id} initialize server {server_id}")
            
        except Exception as e:
            logger.error(f"User Id  {session.user_id} initialize server {server_id} failed: {e}")

async def get_or_create_user_session(
    request: Request,
    auth: HTTPAuthorizationCredentials = Security(security)
):
    """Get or create user session, prioritizing X-User-ID header, and automatically initialize user servers"""
    # First verify API key
    await get_api_key(auth)
    
    # Try to get user ID from request header, if not exist use API key as fallback ID
    user_id = request.headers.get("X-User-ID", auth.credentials)
    
    with session_lock:
        is_new_session = user_id not in user_sessions
        if is_new_session:
            user_sessions[user_id] = UserSession(user_id)
            logger.info(f"Created new session for user {user_id}: {user_sessions[user_id].session_id}")
        
        # Update last active time
        user_sessions[user_id].last_active = datetime.now()
        session = user_sessions[user_id]
    
    # If new session, initialize user's MCP servers
    if is_new_session:
        await initialize_user_servers(session)
    
    return session

async def cleanup_inactive_sessions():
    """Periodically clean up inactive user sessions"""
    while True:
        await asyncio.sleep(300)  # Check every 5 minutes
        current_time = datetime.now()
        inactive_users = []
        
        # Find inactive users
        with session_lock:
            for user_id, session in user_sessions.items():
                if (current_time - session.last_active) > timedelta(minutes=INACTIVE_TIME):
                    inactive_users.append(user_id)
        
        for user_id in inactive_users:
            with session_lock:
                if user_id in user_sessions:
                    session = user_sessions.pop(user_id)
                    try:
                        await session.cleanup()
                    except Exception as e:
                        logger.error(f"Failed to clean up user {user_id} session: {e}")
        
        if inactive_users:
            logger.info(f"Cleaned up {len(inactive_users)} inactive user sessions")

            
class Message(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    messages: List[Message]
    model: str
    max_tokens: int = 4000
    temperature: float = 0.5
    top_p: float = 0.9
    top_k: int = 50
    extra_params : Optional[dict] = {}
    stream: Optional[bool] = None
    tools: Optional[List[dict]] = []
    options: Optional[dict] = {}
    keep_alive: Optional[bool] = None
    mcp_server_ids: Optional[List[str]] = []

class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Dict[str, Any]]
    usage: Dict[str, int]

class AddMCPServerRequest(BaseModel):
    server_id: str = ''
    server_desc: str
    command: Literal["npx", "uvx", "node", "python","docker","uv"] = Field(default='npx')
    args: List[str] = []
    env: Optional[Dict[str, str]] = Field(default_factory=dict) 
    config_json: Dict[str,Any] = Field(default_factory=dict)
    server_url: Optional[str] = None  # For HTTP connections
    http_headers: Optional[Dict[str, str]] = Field(default_factory=dict)  # HTTP headers
    http_timeout: Optional[int] = 30  # HTTP timeout in seconds
    http_sse_timeout: Optional[int] = 300  # HTTP SSE timeout in seconds
    
class AddMCPServerResponse(BaseModel):
    errno: int
    msg: str = "ok"
    data: Dict[str, Any] = Field(default_factory=dict)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Tasks to execute when server starts"""
    # Load persisted user MCP configurations
    await load_user_mcp_configs()
    # Start other initialization tasks
    await startup_event()
    yield
    # Clean up and save state
    await shutdown_event()
    
async def startup_event():
    """Tasks to execute when server starts"""
    # Start session cleanup task
    asyncio.create_task(cleanup_inactive_sessions())

async def shutdown_event():
    """Tasks to execute when server shuts down"""
    # Save user MCP configurations
    await save_user_mcp_configs()
    
    # Clean up all sessions
    cleanup_tasks = []
    with session_lock:
        for user_id, session in user_sessions.items():
            cleanup_tasks.append(session.cleanup())
    
    if cleanup_tasks:
        await asyncio.gather(*cleanup_tasks)
        logger.info(f"Cleaned up all {len(cleanup_tasks)} user sessions")


app = FastAPI(lifespan=lifespan)

# Add CORS middleware to support cross-origin requests and custom headers
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production environment, should be restricted to specific frontend domains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],  # Allow all headers, including custom X-User-ID
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    logger.error(f"Validation error: {exc}")
    return JSONResponse(content=AddMCPServerResponse(
                errno=422,
                msg=str(exc.errors())
            ).model_dump())

@app.get("/v1/list/models")
async def list_models(
    request: Request,
    auth: HTTPAuthorizationCredentials = Security(security)
):
    # Only need to verify API key, no user session required
    await get_api_key(auth)
    return JSONResponse(content={"models": [{
        "model_id": mid, 
        "model_name": name} for mid, name in llm_model_list.items()]})

@app.get("/v1/list/mcp_server")
async def list_mcp_server(
    request: Request,
    auth: HTTPAuthorizationCredentials = Security(security)
):
    # Get user session
    session = await get_or_create_user_session(request, auth)
    
    # Merge global and user-specific server lists
    server_list = {**shared_mcp_server_list}
    
    # Add user-specific servers
    for server_id in session.mcp_clients:
        if server_id not in server_list:
            server_list[server_id] = f"User-specific server: {server_id}"
    
    return JSONResponse(content={"servers": [{
        "server_id": sid, 
        "server_name": name} for sid, name in server_list.items()]})

@app.post("/v1/add/mcp_server")
async def add_mcp_server(
    request: Request,
    data: AddMCPServerRequest,
    background_tasks: BackgroundTasks,
    auth: HTTPAuthorizationCredentials = Security(security)
):
    global shared_mcp_server_list
    # Get user session
    session = await get_or_create_user_session(request, auth)
    user_id = session.user_id
    
    # Use session lock to ensure thread-safe operations
    async with session.lock:
        if data.server_id in session.mcp_clients:
            return JSONResponse(content=AddMCPServerResponse(
                errno=-1,
                msg="MCP server id exists for this user!"
            ).model_dump())
        
        server_id = data.server_id
        server_cmd = data.command
        server_script_args = data.args
        server_script_envs = data.env
        server_desc = data.server_desc if data.server_desc else data.server_id
        
        # Process configuration JSON
        if data.config_json:
            config_json = data.config_json
            if not all([isinstance(k, str) for k in config_json.keys()]):
                return JSONResponse(content=AddMCPServerResponse(
                    errno=-1,
                    msg="env key must be str!"
                ).model_dump())
                
            if "mcpServers" in config_json:
                config_json = config_json["mcpServers"]
                
            server_id = list(config_json.keys())[0]
            server_conf = config_json[server_id]
            
            # Check if this is a remote server configuration
            if "server_url" in server_conf:
                # This is a remote server configuration
                data.server_url = server_conf["server_url"]
                data.http_headers = server_conf.get("http_headers", {})
                data.http_timeout = server_conf.get("http_timeout", 30)
                data.http_sse_timeout = server_conf.get("http_sse_timeout", 300)
            else:
                # This is a local server configuration
                server_cmd = server_conf["command"]
                server_script_args = server_conf["args"]
                server_script_envs = server_conf.get('env',{})
            
        # Connect to MCP server
        mcp_client = MCPClient(name=f"{session.user_id}_{server_id}")
        try:
            # Check if this is an HTTP connection
            if data.server_url and (data.server_url.startswith('http://') or data.server_url.startswith('https://')):
                await mcp_client.connect_to_server(
                    server_url=data.server_url,
                    http_headers=data.http_headers,
                    http_timeout=data.http_timeout,
                    http_sse_timeout=data.http_sse_timeout
                )
            else:
                # Existing connection logic
                await mcp_client.connect_to_server(
                    command=server_cmd,
                    server_script_args=server_script_args,
                    server_script_envs=server_script_envs
                )
            tool_conf = await mcp_client.get_tool_config(server_id=server_id)
            logger.info(f"User {session.user_id} connected to MCP server {server_id}, tools={tool_conf}")
            
            # Save user server configuration for future recovery
            server_config = {
                "description": server_desc
            }
            
            # Check if this is a remote server or local server
            if data.server_url:
                # Remote server configuration
                server_config["server_url"] = data.server_url
                server_config["http_headers"] = data.http_headers
                server_config["http_timeout"] = data.http_timeout
                server_config["http_sse_timeout"] = data.http_sse_timeout
            else:
                # Local server configuration
                server_config["command"] = server_cmd
                server_config["args"] = server_script_args
                server_config["env"] = server_script_envs
                
            save_user_server_config(user_id, server_id, server_config)
            
            #save conf
            await save_user_mcp_configs()
            
        except Exception as e:
            tool_conf = {}
            logger.error(f"User {session.user_id} connect to MCP server {server_id} error: {e}")
            return JSONResponse(content=AddMCPServerResponse(
                errno=-1,
                msg="MCP server connect failed!"
            ).model_dump())

        # Add client to user session
        session.mcp_clients[server_id] = mcp_client
        # Update global server list description
        shared_mcp_server_list[server_id] = server_desc
        await save_user_mcp_configs()
        return JSONResponse(content=AddMCPServerResponse(
            errno=0,
            msg="The server already been added!",
            data={"tools": tool_conf.get("tools", {}) if tool_conf else {}}
        ).model_dump())

@app.delete("/v1/remove/mcp_server/{server_id}")
async def remove_mcp_server(
    server_id: str,
    request: Request,
    auth: HTTPAuthorizationCredentials = Security(security)
):
    """Remove user's MCP server"""
    # Get user session
    session = await get_or_create_user_session(request, auth)
    user_id = session.user_id
    
    # Use session lock to ensure thread-safe operations
    async with session.lock:
        if server_id not in session.mcp_clients:
            return JSONResponse(content=AddMCPServerResponse(
                errno=-1,
                msg="MCP server not found for this user!"
            ).model_dump())
            
        try:
            # Clean up resources
            await session.mcp_clients[server_id].cleanup()
            # Remove server
            del session.mcp_clients[server_id]
            
            # Remove from user configuration
            if user_id in user_mcp_server_configs and server_id in user_mcp_server_configs[user_id]:
                del user_mcp_server_configs[user_id][server_id]
            
            return JSONResponse(content=AddMCPServerResponse(
                errno=0,
                msg="Server removed successfully"
            ).model_dump())
            
        except Exception as e:
            logger.error(f"User {user_id} remove MCP server {server_id} error: {e}")
            return JSONResponse(content=AddMCPServerResponse(
                errno=-1,
                msg=f"Failed to remove server: {str(e)}"
            ).model_dump())

async def stream_chat_response(data: ChatCompletionRequest, session: UserSession) -> AsyncGenerator[str, None]:
    """Generate streaming chat response for specific user"""
    messages = [{
        "role": x.role,
        "content": [{"text": x.content}],
    } for x in data.messages]
    system = []
    if messages and messages[0]['role'] == 'system':
        system = [{"text":messages[0]['content'][0]["text"]}] if messages[0]['content'][0]["text"] else []
        messages = messages[1:]

    # bedrock's first turn cannot be assistant
    if messages and messages[0]['role'] == 'assistant':
        messages = messages[1:]

    try:
        current_content = ""
        thinking_start = False
        thinking_text_index = 0
        
        # Use user-specific chat_client and mcp_clients
        async for response in session.chat_client.process_query_stream(
                model_id=data.model,
                max_tokens=data.max_tokens,
                temperature=data.temperature,
                history=messages,
                system=system,
                max_turns=MAX_TURNS,
                mcp_clients=session.mcp_clients,
                mcp_server_ids=data.mcp_server_ids,
                extra_params=data.extra_params,
                ):
            
            event_data = {
                "id": f"chat{time.time_ns()}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": data.model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": None
                }]
            }
            
            # Handle different event types
            if response["type"] == "message_start":
                event_data["choices"][0]["delta"] = {"role": "assistant"}
            
            elif response["type"] == "block_delta":
                if "text" in response["data"]["delta"]:
                    text = ""
                    if thinking_text_index >= 1 and thinking_start:    
                        thinking_start = False
                        text = "</thinking>"
                    text += response["data"]["delta"]["text"]
                    current_content += text
                    event_data["choices"][0]["delta"] = {"content": text}
                    thinking_text_index = 0
                    
                if "reasoningContent" in response["data"]["delta"]:
                    if 'text' in response["data"]["delta"]["reasoningContent"]:
                        if not thinking_start:
                            text = "<thinking>" + response["data"]["delta"]["reasoningContent"]["text"]
                            thinking_start = True
                        else:
                            text = response["data"]["delta"]["reasoningContent"]["text"]
                        event_data["choices"][0]["delta"] = {"content": text}
                        thinking_text_index += 1
                    
            elif response["type"] == "message_stop":
                event_data["choices"][0]["finish_reason"] = response["data"]["stopReason"]
                if response["data"].get("tool_results"):
                    event_data["choices"][0]["message_extras"] = {
                        "tool_use": json.dumps(response["data"]["tool_results"],ensure_ascii=False)
                    }

            elif response["type"] == "error":
                event_data["choices"][0]["finish_reason"] = "error"
                event_data["choices"][0]["delta"] = {
                    "content": f"Error: {response['data']['error']}"
                }

            # Send event
            yield f"data: {json.dumps(event_data)}\n\n"

            # Send end marker
            if response["type"] == "message_stop" and response["data"]["stopReason"] == 'end_turn':
                yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error(f"Stream error for user {session.user_id}: {e}")
        error_data = {
            "id": f"error{time.time_ns()}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": data.model,
            "choices": [{
                "index": 0,
                "delta": {"content": f"Error: {str(e)}"},
                "finish_reason": "error"
            }]
        }
        yield f"data: {json.dumps(error_data)}\n\n"
        yield "data: [DONE]\n\n"

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request, 
    data: ChatCompletionRequest, 
    background_tasks: BackgroundTasks,
    auth: HTTPAuthorizationCredentials = Security(security)
):
    # Get user session
    session = await get_or_create_user_session(request, auth)
    # Record session activity
    session.last_active = datetime.now()

    if not data.messages:
        return JSONResponse(content=ChatResponse(
            id=f"chat{time.time_ns()}",
            model=data.model,
            created=int(time.time()),
            choices=[{
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "load" 
            }],
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        ).model_dump())

    # Handle streaming request
    if data.stream:
        return StreamingResponse(
            stream_chat_response(data, session),
            media_type="text/event-stream"
        )

    # Handle non-streaming request
    messages = [{
        "role": x.role,
        "content": [{"text": x.content}],
    } for x in data.messages]

    # bedrock's first turn cannot be assistant
    if messages and messages[0]['role'] == 'assistant':
        messages = messages[1:]

    system = []
    if messages and messages[0]['role'] == 'system':
        system = [{"text":messages[0]['content'][0]["text"]}] if messages[0]['content'][0]["text"] else []
        messages = messages[1:]

    try:
        tool_use_info = {}
        async with session.lock:  # Ensure current user's requests are processed in order
            async for response in session.chat_client.process_query(
                    model_id=data.model,
                    max_tokens=data.max_tokens,
                    temperature=data.temperature,
                    history=messages,
                    system=system,
                    max_turns=MAX_TURNS,
                    mcp_clients=session.mcp_clients,
                    mcp_server_ids=data.mcp_server_ids,
                    extra_params=data.extra_params,
                    ):
                logger.info(f"response body for user {session.user_id}: {response}")
                is_tool_use = any([bool(x.get('toolUse')) for x in response['content']])
                is_tool_result = any([bool(x.get('toolResult')) for x in response['content']])
                is_answer = any([bool(x.get('text')) for x in response['content']])

                if is_tool_use:
                    for x in response['content']:
                        if 'toolUse' not in x or not x['toolUse'].get('name'):
                            continue
                        tool_id = x['toolUse'].get('toolUseId')
                        if not tool_id:
                            continue
                        if tool_id not in tool_use_info:
                            tool_use_info[tool_id] = {}
                        tool_use_info[tool_id]['name'] = x['toolUse']['name']
                        tool_use_info[tool_id]['arguments'] = x['toolUse']['input']

                if is_tool_result:
                    for x in response['content']:
                        if 'toolResult' not in x:
                            continue
                        tool_id = x['toolResult'].get('toolUseId')
                        if not tool_id:
                            continue
                        if tool_id not in tool_use_info:
                            tool_use_info[tool_id] = {}
                        tool_use_info[tool_id]['result'] = x['toolResult']['content'][0]['text']

                if is_tool_use or is_tool_result:
                    continue

                chat_response = ChatResponse(
                    id=f"chat{time.time_ns()}",
                    created=int(time.time()),
                    model=data.model,
                    choices=[
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": response['content'][0]['text'],
                            },
                            "message_extras": {
                                "tool_use": [info for too_id, info in tool_use_info.items()],
                            },
                            "logprobs": None,  
                            "finish_reason": "stop", 
                        }
                    ],
                    usage={
                        "prompt_tokens": 0, 
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    }
                )
                
                return JSONResponse(content=chat_response.model_dump())
    except Exception as e:
        logger.error(f"Error processing request for user {session.user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=7002)
    parser.add_argument('--mcp-conf', default='', help="the mcp servers json config file")
    parser.add_argument('--user-conf', default='conf/user_mcp_configs.json', 
                       help="User MCP server configuration file path")
    args = parser.parse_args()
    
    # Set user configuration file path environment variable
    os.environ['USER_MCP_CONFIG_FILE'] = args.user_conf
    
    try:
        loop = asyncio.new_event_loop()

        if args.mcp_conf:
            with open(args.mcp_conf, 'r') as f:
                conf = json.load(f)
                # Load global MCP server configurations
                for server_id, server_conf in conf.get('mcpServers', {}).items():
                    if server_conf.get('status') == 0:
                        continue
                    shared_mcp_server_list[server_id] = server_conf.get('description', server_id)
                    save_global_server_config(server_id, server_conf)

                # Load model configurations
                for model_conf in conf.get('models', []):
                    llm_model_list[model_conf['model_id']] = model_conf['model_name']
        # logger.info(f"shared_mcp_server_list:{shared_mcp_server_list}")
        config = uvicorn.Config(app, host=args.host, port=args.port, loop=loop)
        server = uvicorn.Server(config)
        loop.run_until_complete(server.serve())
    finally:
        # Ensure resources are cleaned up and user configurations are saved on exit
        cleanup_tasks = []
        for user_id, session in user_sessions.items():
            cleanup_tasks.append(session.cleanup())
        
        if cleanup_tasks:
            loop.run_until_complete(asyncio.gather(*cleanup_tasks))
        
        # Save user configurations
        try:
            loop.run_until_complete(save_user_mcp_configs())
        except:
            pass
        
        loop.close()
