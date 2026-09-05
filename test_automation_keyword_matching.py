from services.automation_keywords import message_contains_keyword


def test_short_trigger_keyword_does_not_match_inside_customer_reply():
    assert message_contains_keyword("Hi, can you help?", "hi")
    assert message_contains_keyword("Please share the support options", "support")
    assert message_contains_keyword("Hello, I am interested in Custom PC Built.", "hello, I am interested")
    assert not message_contains_keyword("All ok pn mla 1tb ssd pahijel is it possible", "hi")
    assert not message_contains_keyword("Is this available?", "hi")


if __name__ == "__main__":
    test_short_trigger_keyword_does_not_match_inside_customer_reply()
    print("Automation keyword checks passed.")
