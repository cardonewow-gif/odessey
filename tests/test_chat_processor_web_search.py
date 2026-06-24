from unittest.mock import MagicMock
from types import SimpleNamespace
from src.chat_processor import ChatProcessor


def test_build_context_preface_web_search_query_generation(monkeypatch):
    """Test that build_context_preface correctly extracts a web search query via LLM."""

    # Stub dependencies
    mock_llm_call = MagicMock(return_value="extracted query")
    monkeypatch.setattr("src.llm_core.llm_call", mock_llm_call)

    mock_web_search = MagicMock(return_value=("Search Results Mock", [{"url": "http://mock.com"}]))
    monkeypatch.setattr("src.chat_processor.comprehensive_web_search", mock_web_search)



    processor = ChatProcessor(
        memory_manager=MagicMock(),
        personal_docs_manager=MagicMock(),
    )

    session = SimpleNamespace(endpoint_url="http://fallback.local", model="fallback-model", headers={})
    user_message = "This is a complex message.\n\nI just want to search about LLMs."

    preface, rag, web = processor.build_context_preface(
        message=user_message,
        session=session,
        use_web=True,
        use_rag=False,
        use_memory=False,
        use_skills=False
    )

    # 1. LLM should be called to extract query
    assert mock_llm_call.call_count == 1
    llm_args, llm_kwargs = mock_llm_call.call_args
    assert "LLMs" in llm_args[2][1]["content"]  # The user message was passed

    # 2. Web search should be called with the extracted query
    assert mock_web_search.call_count == 1
    mock_web_search.assert_called_with("extracted query", time_filter=None, return_sources=True)

    # 3. Web sources are returned
    assert web == [{"url": "http://mock.com"}]
    assert any("Search Results Mock" in str(p.get("content", "")) for p in preface)


def test_build_context_preface_web_search_fallback_on_llm_failure(monkeypatch):
    """Test that if LLM query generation fails, web search falls back to the first line of the prompt."""

    def failing_llm_call(*args, **kwargs):
        raise ValueError("Model server down")

    monkeypatch.setattr("src.llm_core.llm_call", failing_llm_call)

    mock_web_search = MagicMock(return_value=("Search Results Mock", []))
    monkeypatch.setattr("src.chat_processor.comprehensive_web_search", mock_web_search)



    processor = ChatProcessor(
        memory_manager=MagicMock(),
        personal_docs_manager=MagicMock(),
    )

    session = SimpleNamespace(endpoint_url="http://fallback", model="fallback-model", headers={})
    user_message = "First line fallback query\n\nSome more text that shouldn't be searched."

    preface, rag, web = processor.build_context_preface(
        message=user_message,
        session=session,
        use_web=True,
        use_rag=False,
        use_memory=False,
        use_skills=False
    )

    # Web search should STILL be called, but using the sanitized fallback query
    assert mock_web_search.call_count == 1
    mock_web_search.assert_called_with("First line fallback query", time_filter=None, return_sources=True)
