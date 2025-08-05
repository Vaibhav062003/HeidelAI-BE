import os
from dotenv import load_dotenv 
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()

os.environ["GEMINI_API_KEY"] = os.getenv('GOOGLE_API_KEY')

def get_llm(
    model: str = None,
    temperature: float = 0,
    max_retries: int = 2,
    google_api_key: str = None,
    structured_output_schema: type = None
):
    """Returns a configured LLM instance, optionally with structured output."""
    model = model or os.getenv("LLM_MODEL")
    google_api_key = google_api_key or os.getenv("GEMINI_API_KEY")

    llm = ChatGoogleGenerativeAI(
        model=model,
        temperature=temperature,
        max_retries=max_retries,
        google_api_key=google_api_key,
    )

    if structured_output_schema:
        return llm.with_structured_output(structured_output_schema)

    return llm

if __name__ == "__main__":
    pass

