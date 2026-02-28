import paramiko
key = paramiko.RSAKey.generate(2048)
key.write_private_key_file("./fsdgveadsvascacxafxdsfxasfxqwqwe1234324exq")
with open("./fsdgveadsvascacxafxdsfxasfxqwqwe1234324exq.pub", "w") as f:
    f.write(f"{key.get_name()} {key.get_base64()} fastapi-key")
print("Готово! Проверь папку проекта.")

from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())