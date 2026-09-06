"""Tests for agent.win_ca_bundle (written FIRST per TDD)."""
import base64
import ssl
from pathlib import Path

import certifi
import pytest


@pytest.mark.windows_only
def test_real_store_loads():
    from agent.win_ca_bundle import windows_merged_ca_bundle
    result = windows_merged_ca_bundle()
    # On native Windows: returns a path that exists and loads >= 1 cert.
    # On non-Windows this test is skipped by the marker.
    assert result is not None
    p = Path(result)
    assert p.exists()
    ctx = ssl.create_default_context(cafile=str(p))
    try:
        certs = ctx.get_ca_certs()
        assert len(certs) >= 1
    except NotImplementedError:
        pass  # truststore-backed; creation success sufficient


def test_gate_non_win32(monkeypatch):
    import agent.win_ca_bundle as m
    monkeypatch.setattr(m.sys, "platform", "darwin")
    assert m.windows_merged_ca_bundle() is None


def test_pure_pem_helper_includes_serverauth_excludes_others():
    from agent.win_ca_bundle import _pem_from_store_entries
    # Read one real PEM block from certifi, decode to DER
    cert_path = certifi.where()
    pem_content = Path(cert_path).read_text()
    # Extract first PEM block
    lines = pem_content.splitlines()
    start_idx = lines.index("-----BEGIN CERTIFICATE-----")
    end_idx = lines.index("-----END CERTIFICATE-----", start_idx)
    pem_block = "\n".join(lines[start_idx:end_idx + 1]) + "\n"
    der_bytes = base64.b64decode("".join(lines[start_idx + 1:end_idx]))

    # Included: x509_asn + serverAuth
    included = _pem_from_store_entries([(der_bytes, "x509_asn", True)])
    assert len(included) == 1
    assert "BEGIN CERTIFICATE" in included[0]

    # Excluded: pkcs_7_asn
    excluded_pkcs = _pem_from_store_entries([(der_bytes, "pkcs_7_asn", True)])
    assert excluded_pkcs == []

    # Excluded: non-serverAuth OID tuple
    excluded_oid = _pem_from_store_entries([(der_bytes, "x509_asn", ("1.3.6.1.5.2.3.4",))])
    assert excluded_oid == []

    # Included: serverAuth OID tuple
    included_oid = _pem_from_store_entries([(der_bytes, "x509_asn", ("1.3.6.1.5.5.7.3.1",))])
    assert len(included_oid) == 1

    # Dedup: same DER twice -> one PEM
    deduped = _pem_from_store_entries([
        (der_bytes, "x509_asn", True),
        (der_bytes, "x509_asn", True),
    ])
    assert len(deduped) == 1


def test_pem_loads_via_ssl_create_default_context():
    from agent.win_ca_bundle import _pem_from_store_entries
    cert_path = certifi.where()
    pem_content = Path(cert_path).read_text()
    lines = pem_content.splitlines()
    start = lines.index("-----BEGIN CERTIFICATE-----")
    end = lines.index("-----END CERTIFICATE-----", start)
    der_bytes = base64.b64decode("".join(lines[start + 1:end]))

    pem_blocks = _pem_from_store_entries([(der_bytes, "x509_asn", True)])
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        for block in pem_blocks:
            f.write(block)
        tmp_path = f.name

    ctx = ssl.create_default_context(cafile=tmp_path)
    try:
        certs = ctx.get_ca_certs()
        assert len(certs) >= 1
    except NotImplementedError:
        pass
    Path(tmp_path).unlink(missing_ok=True)
