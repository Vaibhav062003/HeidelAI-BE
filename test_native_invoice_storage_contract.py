from pathlib import Path


def test_native_invoice_storage_is_private_immutable_and_hashed():
    source = Path("services/cloudinary_service.py").read_text()
    assert "hashlib.sha256(data).hexdigest()" in source
    assert "overwrite=False" in source
    assert 'type="private"' in source
    assert '"renderer_version": renderer_version' in source
    assert '"issued_revision": issued_revision' in source
    assert 'def native_invoice_pdf_download_url(pdf: dict, expires_in_seconds: int = 60)' in source
    assert "is not a verified server artifact" in source


def test_irp_signed_artifacts_are_not_exposed_by_invoice_api():
    source = Path("services/native_invoice_service.py").read_text()
    assert 'value.pop("irp_artifact", None)' in source
    irp_source = Path("services/iris_irp_service.py").read_text()
    assert '"signed_invoice_encrypted"' in irp_source
    assert '"signed_qr_encrypted"' in irp_source


if __name__ == "__main__":
    test_native_invoice_storage_is_private_immutable_and_hashed()
    test_irp_signed_artifacts_are_not_exposed_by_invoice_api()
    print("Native invoice storage contract passed.")
