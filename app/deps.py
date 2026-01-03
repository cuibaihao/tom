from langchain_community.embeddings import ZhipuAIEmbeddings
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from app.config import settings
from app.rag.vectorstore import get_vectorstore, get_audio_vectorstore
from zhipuai import ZhipuAI

def get_llm():
    """
    获取大语言模型 ———— 使用的是deepseek
    """
    return ChatOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.base_url,
        model=settings.model_name,
        streaming=True,
        temperature=0
    )

def get_embeddings():
    """
    获取嵌入式模型
    """
    return ZhipuAIEmbeddings(
        client=ZhipuAI(api_key=settings.zhipuai_api_key,
                       base_url="https://open.bigmodel.cn/api/paas/v4/embeddings"),
        model="embedding-3",
        api_key=settings.zhipuai_api_key
    )

def get_vs():
    return get_vectorstore(get_embeddings())

def get_audio_vs():
    return get_audio_vectorstore(get_embeddings())


if __name__ == "__main__":

    print('------------------------------------')
    print(get_llm())
    print('------------------------------------')
    print(get_vs())

