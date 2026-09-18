"""生成仅用于本机/实验内网的自签名TLS证书。"""

import argparse
import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/tls"))
    parser.add_argument("--hostname", action="append", default=[])
    parser.add_argument("--ip", action="append", default=[])
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = output / "server.crt", output / "server.key"
    if cert_path.exists() or key_path.exists():
        raise FileExistsError("证书或私钥已存在，拒绝覆盖")
    hostnames = sorted(set(args.hostname or ["localhost"]))
    ips = sorted(set(args.ip or ["127.0.0.1"]))
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Wenjin Development"),
        x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0]),
    ])
    now = datetime.now(timezone.utc)
    san = [x509.DNSName(name) for name in hostnames]
    san.extend(x509.IPAddress(ipaddress.ip_address(value)) for value in ips)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"certificate={cert_path}")
    print(f"private_key={key_path}")
    print("仅用于开发验收；私钥不得提交Git。")


if __name__ == "__main__":
    main()
