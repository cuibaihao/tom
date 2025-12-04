
# main.py
from fastapi import FastAPI, Form


app = FastAPI()

@app.post("/login")
async def login(
        username: str = Form(...),
        password: str = Form(...)
):
    # 在后台打印用户名和密码
    print(f"用户名：{username}")
    print(f"密码：{password}")

    # 返回一个简单的响应给前端
    return {"message": "OK SUCCESS", "username": username}



# 这个python程序可以叫做main.py
# 之后，在这个文件所在的路径打开一个终端（输入ls可以卡看到main.py这个结果），输入
# uvicorn main:app --reload

# curl -X POST "http://127.0.0.1:8000/login" \
#   -H "Content-Type: application/x-www-form-urlencoded" \
#   -d "username=zhangsan&password=12345678"

# curl用于帮我们调用任何一个后台代码，检查输出

