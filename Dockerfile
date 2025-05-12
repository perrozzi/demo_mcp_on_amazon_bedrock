FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (v22.12.0)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g npm@latest

# Verify Node.js version
RUN node --version && npm --version

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Make sure uv is in the PATH and executable
RUN echo "PATH=$PATH" && \
    which uv || echo "uv not found in PATH" && \
    uv --version

# Clone the repository
COPY . /app/

# Create Python virtual environment and install dependencies
RUN uv sync

# Activate virtual environment in the shell
ENV PATH="/app/.venv/bin:${PATH}"
ENV VIRTUAL_ENV="/app/.venv"

# Create necessary directories
RUN mkdir -p /app/logs /app/tmp

# Set default environment variables
# These can be overridden when running the container
ENV AWS_REGION="us-east-1" \
    LOG_DIR="./logs" \
    CHATBOT_SERVICE_PORT="8502" \
    MCP_SERVICE_HOST="127.0.0.1" \
    MCP_SERVICE_PORT="7002" \
    API_KEY="123456" \
    MAX_TURNS="100"

# Create a startup script
RUN echo '#!/bin/bash\n\
# Start the MCP service\n\
bash start_all.sh\n\
\n\
# Keep container running\n\
tail -f /app/logs/start_mcp.log\n\
' > /app/start.sh && chmod +x /app/start.sh

# Expose the ports
EXPOSE 7002 8502

# Set the entrypoint
ENTRYPOINT ["/app/start.sh"]
