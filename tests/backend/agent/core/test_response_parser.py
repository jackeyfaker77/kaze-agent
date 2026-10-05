import pytest
from agent.core.response_parser import parse_response


@pytest.mark.parametrize("text", ["普通回复", '{"content":"原样保留","mood":"高兴"}', "```json\n{}\n```", ""])
def test_response_parser_preserves_user_visible_content(text):
    result = parse_response(text, tool_chain=[])
    assert result.clean_text == text
    assert result.metadata.raw_text == text
