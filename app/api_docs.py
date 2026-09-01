"""OpenAPI / Swagger UI 中文文档配置。"""

OPENAPI_TAGS = [
    {
        "name": "影像服务",
        "description": "影像切片任务的创建、查询、发布与工作区浏览。",
    },
    {
        "name": "影像服务 · WebSocket",
        "description": "任务进度实时推送。",
    },
    {
        "name": "系统",
        "description": "服务健康检查。",
    },
]

SWAGGER_UI_PARAMETERS = {
    "locale": "zh-CN",
    "docExpansion": "list",
    "defaultModelsExpandDepth": -1,
    "filter": True,
    "tryItOutEnabled": True,
    "displayRequestDuration": True,
    "syntaxHighlight.theme": "monokai",
}
