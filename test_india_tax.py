from services.india_tax import gst_state_name, gstin_check_digit, is_valid_gstin


def test_gstin_checksum_and_state_names():
    assert gstin_check_digit("27AAPFU0939F1Z") == "V"
    assert is_valid_gstin("27AAPFU0939F1ZV")
    assert is_valid_gstin("27aapfu0939f1zv")
    assert not is_valid_gstin("27AAPFU0939F1ZA")
    assert gst_state_name("27") == "Maharashtra"


if __name__ == "__main__":
    test_gstin_checksum_and_state_names()
    print("India GST helpers passed.")
