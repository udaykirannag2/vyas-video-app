#!/usr/bin/env python3
"""CDK app entrypoint. All resources are tagged app=vyas-video for cost allocation."""
import os

import aws_cdk as cdk

from stacks.frontend_stack import FrontendStack
from stacks.api_stack import ApiStack
from stacks.render_stack import RenderStack

APP_TAG = "vyas-video"

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

render = RenderStack(app, "VyasVideoRender", env=env)
api = ApiStack(
    app,
    "VyasVideoApi",
    env=env,
    assets_bucket=render.assets_bucket,
    render_state_machine=render.state_machine,
    table=render.table,
)
frontend = FrontendStack(app, "VyasVideoFrontend", env=env, api_url=api.api_url)

# Propagate tag to every resource in the app.
cdk.Tags.of(app).add("app", APP_TAG)

app.synth()
