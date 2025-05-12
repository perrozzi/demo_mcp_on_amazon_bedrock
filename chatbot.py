"""
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
import os
import re
import json
import time
import html
import logging
import requests
import streamlit as st
import base64
import uuid
from io import BytesIO
from streamlit_local_storage import LocalStorage
import copy
from dotenv import load_dotenv
load_dotenv() # load env vars from .env
API_KEY = os.environ.get("API_KEY")

logging.basicConfig(level=logging.INFO)
mcp_base_url = os.environ.get('MCP_BASE_URL')
mcp_command_list = ["uvx", "npx", "node", "python","docker","uv"]
COOKIE_NAME = "mcp_chat_user_id"
local_storage = LocalStorage()
# User session management
def initialize_user_session():
    """Initialize user session, ensuring each user has a unique identifier"""    
    # Try to get user ID from cookie
    if "user_id" not in st.session_state:
        if local_storage and local_storage.getItem(COOKIE_NAME):
            st.session_state.user_id = local_storage.getItem(COOKIE_NAME)
            logging.info(f"Retrieved user ID: {st.session_state.user_id}")
            return
        else:
            # Generate new user ID
            st.session_state.user_id = str(uuid.uuid4())[:8]
            # Save to LocalStorage
            local_storage.setItem(COOKIE_NAME, st.session_state.user_id)
    
# Function to generate random user ID
def generate_random_user_id():
    st.session_state.user_id = str(uuid.uuid4())[:8]
    # Update cookie
    local_storage.setItem(COOKIE_NAME, st.session_state.user_id)
    logging.info(f"Generated new random user ID: {st.session_state.user_id}")
    
# Save to cookie when user manually changes ID
def save_user_id():
    st.session_state.user_id = st.session_state.user_id_input
    local_storage.setItem(COOKIE_NAME, st.session_state.user_id)
    logging.info(f"Saved user ID: {st.session_state.user_id}")

initialize_user_session()
    
def get_auth_headers():
    """Build authentication headers containing user identity"""
    headers = {
        'Authorization': f'Bearer {API_KEY}',
        'X-User-ID': st.session_state.user_id  # Add user ID header
    }
    return headers

def request_list_models():
    url = mcp_base_url.rstrip('/') + '/v1/list/models'
    models = []
    try:
        response = requests.get(url, headers=get_auth_headers())
        data = response.json()
        models = data.get('models', [])
    except Exception as e:
        logging.error('request list models error: %s' % e)
    return models

def request_list_mcp_servers():
    url = mcp_base_url.rstrip('/') + '/v1/list/mcp_server'
    mcp_servers = []
    try:
        response = requests.get(url, headers=get_auth_headers())
        data = response.json()
        mcp_servers = data.get('servers', [])
    except Exception as e:
        logging.error('request list mcp servers error: %s' % e)
    return mcp_servers

def request_add_mcp_server(server_id, server_name, command, args=[], env=None, config_json={}):
    url = mcp_base_url.rstrip('/') + '/v1/add/mcp_server'
    status = False
    try:
        payload = {
            "server_id": server_id,
            "server_desc": server_name,
            "command": command,
            "args": args,
            "config_json": config_json
        }
        if env:
            payload["env"] = env
        response = requests.post(url, json=payload, headers=get_auth_headers())
        data = response.json()
        status = data['errno'] == 0
        msg = data['msg']
    except Exception as e:
        msg = "Add MCP server occurred errors!"
        logging.error('request add mcp servers error: %s' % e)
    return status, msg

def process_stream_response(response):
    """Process streaming response and yield content chunks"""
    for line in response.iter_lines():
        if line:
            line = line.decode('utf-8')
            if line.startswith('data: '):
                data = line[6:]  # Remove 'data: ' prefix
                if data == '[DONE]':
                    break
                try:
                    json_data = json.loads(data)
                    delta = json_data['choices'][0].get('delta', {})
                    if 'role' in delta:
                        continue
                    if 'content' in delta:
                        yield delta['content']
                    
                    message_extras = json_data['choices'][0].get('message_extras', {})
                    if "tool_use" in message_extras:
                        yield f"<tool_use>{message_extras['tool_use']}</tool_use>"

                except json.JSONDecodeError:
                    logging.error(f"Failed to parse JSON: {data}")
                except Exception as e:
                    logging.error(f"Error processing stream: {e}")

def request_chat(messages, model_id, mcp_server_ids, stream=False, max_tokens=1024, temperature=0.6, extra_params={}):
    url = mcp_base_url.rstrip('/') + '/v1/chat/completions'
    msg, msg_extras = 'something is wrong!', {}
    try:
        payload = {
            'messages': messages,
            'model': model_id,
            'mcp_server_ids': mcp_server_ids,
            'extra_params': extra_params,
            'stream': stream,
            'temperature': temperature,
            'max_tokens': max_tokens
        }
        logging.info(f'User {st.session_state.user_id} request payload: %s' % payload)
        
        if stream:
            # Streaming request
            headers = get_auth_headers()
            headers['Accept'] = 'text/event-stream'  
            response = requests.post(url, json=payload, stream=True, headers=headers)
            
            if response.status_code == 200:
                return response, {}
            else:
                msg = 'An error occurred when calling the Converse operation: The system encountered an unexpected error during processing. Try your request again.'
                logging.error(f'User {st.session_state.user_id} chat request error: %d' % response.status_code)
        else:
            # Regular request
            response = requests.post(url, json=payload, headers=get_auth_headers())
            data = response.json()
            msg = data['choices'][0]['message']['content']
            msg_extras = data['choices'][0]['message_extras']

    except Exception as e:
        msg = 'An error occurred when calling the Converse operation: The system encountered an unexpected error during processing. Try your request again.'
        logging.error(f'User {st.session_state.user_id} chat request error: %s' % e)
    
    logging.info(f'User {st.session_state.user_id} response message: %s' % msg)
    return msg, msg_extras

# Initialize session state
if not 'model_names' in st.session_state:
    st.session_state.model_names = {}
    for x in request_list_models():
        st.session_state.model_names[x['model_name']] = x['model_id']

if not 'mcp_servers' in st.session_state:
    st.session_state.mcp_servers = {}
    for x in request_list_mcp_servers():
        st.session_state.mcp_servers[x['server_name']] = x['server_id']

if "system_prompt" not in st.session_state:
    st.session_state.system_prompt = "You are a deep researcher"

if "messages" not in st.session_state:
    st.session_state.messages = []
    
# Message list always stays in sync with current system_prompt
if not st.session_state.messages or st.session_state.messages[0]["role"] != "system":
    st.session_state.messages.insert(0, {"role": "system", "content": st.session_state.system_prompt})
else:
    st.session_state.messages[0]["content"] = st.session_state.system_prompt 

if "enable_stream" not in st.session_state:
    st.session_state.enable_stream = True
    
if "enable_thinking" not in st.session_state:
    st.session_state.enable_thinking = False


    
# Function to clear conversation history
def clear_conversation():
    st.session_state.messages = [
        {"role": "system", "content": st.session_state.system_prompt},
    ]
    st.session_state.should_rerun = True

# Check if we need to rerun the app
if "should_rerun" not in st.session_state:
    st.session_state.should_rerun = False
if st.session_state.should_rerun:
    st.session_state.should_rerun = False
    st.rerun()

# add new mcp UI and handle
def add_new_mcp_server_handle():
    status, msg = True, "The server already been added!"
    server_name = st.session_state.new_mcp_server_name
    server_id = st.session_state.new_mcp_server_id
    server_cmd = st.session_state.new_mcp_server_cmd
    server_args = st.session_state.new_mcp_server_args
    server_env = st.session_state.new_mcp_server_env
    server_config_json = st.session_state.new_mcp_server_json_config
    config_json = {}
    if not server_name:
        status, msg = False, "The server name is empty!"
    elif server_name in st.session_state.mcp_servers:
        status, msg = False, "The server name exists, try another name!"

    # If server_config_json is configured, use it as the source of truth
    if server_config_json:
        try:
            config_json = json.loads(server_config_json)
            if not all([isinstance(k, str) for k in config_json.keys()]):
                raise ValueError("env key must be str.")
            if "mcpServers" in config_json:
                config_json = config_json["mcpServers"]
            # Use ID directly from JSON config
            logging.info(f'User {st.session_state.user_id} adding new MCP server: {config_json}')
            server_id = list(config_json.keys())[0]
            server_cmd = config_json[server_id]["command"]
            server_args = config_json[server_id]["args"]
            server_env = config_json[server_id].get('env')
        except Exception as e:
            status, msg = False, "The config must be a valid JSON."

    if not re.match(r'^[a-zA-Z][a-zA-Z0-9_]*$', server_id):
        status, msg = False, "The server id must be a valid variable name!"
    elif server_id in st.session_state.mcp_servers.values():
        status, msg = False, "The server id exists, try another one!"
    elif not server_cmd or server_cmd not in mcp_command_list:
        status, msg = False, "The server command is invalid!"
    if server_env:
        try:
            server_env = json.loads(server_env) if not isinstance(server_env, dict) else server_env
            if not all([isinstance(k, str) for k in server_env.keys()]):
                raise ValueError("env key must be str.")
            if not all([isinstance(v, str) for v in server_env.values()]):
                raise ValueError("env value must be str.")
        except Exception as e:
            server_env = {}
            status, msg = False, "The server env must be a JSON dict[str, str]."
    if isinstance(server_args, str):
        server_args = [x.strip() for x in server_args.split(' ') if x.strip()]

    logging.info(f'User {st.session_state.user_id} adding new MCP server: {server_id}:{server_name}')
    
    with st.spinner('Add the server...'):
        status, msg = request_add_mcp_server(server_id, server_name, server_cmd, 
                                             args=server_args, env=server_env, config_json=config_json)
    if status:
        st.session_state.mcp_servers[server_name] = server_id

    st.session_state.new_mcp_server_fd_status = status
    st.session_state.new_mcp_server_fd_msg = msg


@st.dialog('MCP Server Configuration')
def add_new_mcp_server():
    with st.form("my_form"):
        st.write("**Add New MCP Server**")

        if 'new_mcp_server_fd_status' in st.session_state:
            if st.session_state.new_mcp_server_fd_status:
                succ1 = st.success(st.session_state.new_mcp_server_fd_msg, icon="✅")
                succ2 = st.success("Please **refresh** the page to display it.", icon="📒")
                time.sleep(3)
                succ1.empty()
                succ2.empty()
                st.session_state.new_mcp_server_fd_msg = ""
                st.session_state.new_mcp_server_id = ""
                st.session_state.new_mcp_server_name = ""
                st.session_state.new_mcp_server_args = ""
                st.session_state.new_mcp_server_env = ""
                st.session_state.new_mcp_server_json_config = ""
            else:
                if st.session_state.new_mcp_server_fd_msg:
                    st.error(st.session_state.new_mcp_server_fd_msg, icon="🚨")

        new_mcp_server_name = st.text_input("Server Name", 
                                            value="", placeholder="Name description of server", key="new_mcp_server_name")
        
        new_mcp_server_config_json = st.text_area("Use JSON Configuration", 
                                    height=128,
                                    value="", key="new_mcp_server_json_config",
                                    placeholder="Need to provide a valid JSON dictionary")
        with st.expander(label='Input Field Configuration', expanded=False):
            new_mcp_server_id = st.text_input("Server ID", 
                                            value="", placeholder="server id", key="new_mcp_server_id")

            new_mcp_server_cmd = st.selectbox("Run Command", 
                                            mcp_command_list, key="new_mcp_server_cmd")
            new_mcp_server_args = st.text_area("Run Arguments", 
                                            value="", key="new_mcp_server_args",
                                            placeholder="mcp-server-git --repository path/to/git/repo")
            new_mcp_server_env = st.text_area("Environment Variables", 
                                            value="", key="new_mcp_server_env",
                                            placeholder="Need to provide a valid JSON dictionary")

        submitted = st.form_submit_button("Add", 
                                          on_click=add_new_mcp_server_handle,
                                          disabled=False)

def on_system_prompt_change():
    if st.session_state.messages[0]["role"] == "system":
        st.session_state.messages[0]["content"] = st.session_state.system_prompt
        
# UI
with st.sidebar:
    col1, col2 = st.columns([3, 1])
    with col1:
        st.session_state.user_id = st.text_input('User ID', key='user_id_input',value=st.session_state.user_id,on_change=save_user_id, max_chars=32)
    with col2:
        st.button("🔄", on_click=generate_random_user_id, help="Generate random user ID")

    llm_model_name = st.selectbox('Model List',
                                  list(st.session_state.model_names.keys()))
    st.session_state.max_tokens = st.number_input('Max output token',
                                 min_value=1, max_value=64000, value=8000)
    st.session_state.budget_tokens = st.number_input('Max thinking token',
                                 min_value=1024, max_value=128000, value=8192,step=1024)
    st.session_state.temperature = st.number_input('Temperature',
                                 min_value=0.0, max_value=1.0, value=0.6, step=0.1)
    st.session_state.only_n_most_recent_images = st.number_input('N most recent images',
                                 min_value=0, value=1)
    st.session_state.system_prompt = st.text_area('System',
                                value=st.session_state.system_prompt,
                                height=100,
                                on_change=on_system_prompt_change,
                                )
    st.session_state.enable_thinking = st.toggle('Thinking', value=False)

    st.session_state.enable_stream = st.toggle('Stream', value=True)
    with st.expander(label='Existing MCP Servers', expanded=True):
        for i, server_name in enumerate(st.session_state.mcp_servers):
            st.checkbox(label=server_name, value=False, key=f'mcp_server_{server_name}')
    st.button("Add MCP Server", 
              on_click=add_new_mcp_server)
    
    with st.container():
        st.button("🗑️ Clear Context", on_click=clear_conversation, key="clear_button")

st.title("💬 Bedrock Chatbot with MCP")

# Display chat messages
for msg in st.session_state.messages:
    st.chat_message(msg["role"]).write(msg["content"])

# Handle user input
if prompt := st.chat_input():
    # Update system message
    st.session_state.messages[0] = {"role": "system", "content": st.session_state.system_prompt}
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.chat_message("user").write(prompt)

    model_id = st.session_state.model_names[llm_model_name]
    mcp_server_ids = []
    for server_name in st.session_state.mcp_servers:
        server_key = f'mcp_server_{server_name}'
        if st.session_state.get(server_key):
            mcp_server_ids.append(st.session_state.mcp_servers[server_name])

    # Create a placeholder for the assistant's response
    with st.chat_message("assistant"):
        response_placeholder = st.empty()
        full_response = ""
        response, msg_extras = request_chat(st.session_state.messages, model_id, 
                        mcp_server_ids, stream=st.session_state.enable_stream,
                        max_tokens=st.session_state.max_tokens,
                        temperature=st.session_state.temperature, extra_params={
                            "only_n_most_recent_images": st.session_state.only_n_most_recent_images,
                            "budget_tokens": st.session_state.budget_tokens,
                            "enable_thinking": st.session_state.enable_thinking
                        }
                    )
        # Get streaming response
        if st.session_state.enable_stream:
            if isinstance(response, requests.Response):
                # Process streaming response
                tool_count = 1
                content_block_idx = 0
                thinking_content = ""  # Add variable to store accumulated thinking content
                thinking_expander = None  # For storing thinking expander object
                for content in process_stream_response(response):
                    # logging.info(f"content block idx:{content_block_idx}")
                    content_block_idx += 1
                    full_response += content
                    thk_msg, res_msg, tool_msg = "", "", ""
                    thk_regex = r"<thinking>(.*?)</thinking>"
                    tooluse_regex = r"<tool_use>(.*?)</tool_use>"
                    thk_m = re.search(thk_regex, full_response, re.DOTALL)
                    if thk_m:
                        thk_msg = thk_m.group(1)
                        full_response = re.sub(thk_regex, "", full_response,flags=re.DOTALL)
                        # If there's new thinking content, append to existing content
                        if thk_msg != thinking_content:
                            thinking_content = thk_msg  # Update thinking content
                            # Create expander if it doesn't exist, otherwise update existing one
                            if thinking_expander is None:
                                thinking_expander = st.expander("Thinking")
                            with thinking_expander:
                                st.write(thinking_content)

                    tool_m = re.search(tooluse_regex, full_response, re.DOTALL)
                    if tool_m:
                        tool_msg = tool_m.group(1)
                        full_response = re.sub(tooluse_regex, "", full_response)
                    if tool_msg:
                        # with st.expander("Tool Used"):
                        with st.container(border=True):
                            tool_blocks = json.loads(tool_msg)
                            for i,tool_block in enumerate(tool_blocks):
                                if i%2 == 0:
                                    with st.expander(f"Tool Call:{tool_count}"):
                                        # st.json(tool_block)
                                        st.code(json.dumps(tool_block, ensure_ascii=False, indent=2), language="json")
                                else:
                                    with st.expander(f"Tool Result:{tool_count}"):
                                         # Process image data
                                        images_data = []
                                        display_tool_block = copy.deepcopy(tool_block)  # Create copy for modification
                                        
                                        # If there's a content field, process images within it
                                        if 'content' in display_tool_block:
                                            for j, block in enumerate(display_tool_block['content']):
                                                if 'image' in block and 'source' in block['image'] and 'base64' in block['image']['source']:
                                                    # Save image data for later display
                                                    images_data.append(BytesIO(base64.b64decode(block['image']['source']['base64'])))
                                                    # Replace base64 string with info message
                                                    display_tool_block['content'][j]['image']['source']['base64'] = "[BASE64 IMAGE DATA - NOT DISPLAYED]"
                                        
                                        # Display processed JSON
                                        st.code(json.dumps(display_tool_block, ensure_ascii=False, indent=2), language="json")
                
                                        # Display images
                                        tool_count += 1
                                        for image_data in images_data:
                                            st.image(image_data)

                    # Update response in real-time
                    response_placeholder.markdown(full_response + "▌")
                
                # Update final response without cursor
                response_placeholder.markdown(full_response)
            else:
                # Handle error case
                response_placeholder.markdown(response)
                full_response = response
        else:
            tool_msg = ""
            if msg_extras.get('tool_use', []):
                tool_msg = f"```\n{json.dumps(msg_extras.get('tool_use', []), indent=4,ensure_ascii=False)}\n```"
            thk_msg, res_msg = "", ""
            thk_regex = r"<thinking>(.*?)</thinking>"
            thk_m = re.search(thk_regex, response, re.DOTALL)
            if thk_m:
                thk_msg = thk_m.group(1)

            res_msg = re.sub(thk_regex, "", response)
            st.write(res_msg)

            if thk_msg:
                with st.expander("Thinking"):
                    st.write(thk_msg)
            if tool_msg:
                with st.expander("Tool Used"):
                    st.json(tool_msg)

            full_response = response 

    # Add assistant's response to chat history
    st.session_state.messages.append({"role": "assistant", "content": full_response})
