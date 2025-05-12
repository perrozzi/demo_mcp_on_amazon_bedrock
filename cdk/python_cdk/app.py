#!/usr/bin/env python3
import os
import aws_cdk as cdk
from bedrock_mcp.bedrock_mcp_stack import BedrockMcpStack

app = cdk.App()

# Get qualifier from context or CDK_QUALIFIER env var (set by --qualifier)
qualifier = app.node.try_get_context('qualifier') or os.environ.get('CDK_QUALIFIER')

# Require either --context qualifier or --qualifier
if not qualifier:
    raise ValueError("Qualifier must be provided via --context qualifier=<value> or --qualifier=<value>")

# Environment configuration
env = cdk.Environment(
    account=os.environ.get('CDK_DEFAULT_ACCOUNT'),
    region=os.environ.get('CDK_DEFAULT_REGION', 'us-east-1')
)

# Stack configuration
stack_props = {
    'env': env,
    'description': 'Bedrock MCP Demo Stack',
    'synthesizer': cdk.DefaultStackSynthesizer(
        qualifier=qualifier,
        bootstrap_stack_version_ssm_parameter=f'/cdk-bootstrap/{qualifier}/version',
        file_assets_bucket_name=f'cdk-{qualifier}-assets-{env.account}-{env.region}'
    )
}

# Create stack with qualifier
BedrockMcpStack(app, f'BedrockMcpStack-{qualifier}', **stack_props)

app.synth()
