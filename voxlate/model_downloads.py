"""Pinned, already converted translation weights; no PyTorch preparation step."""
TRANSLATION_REPO = "jncraton/m2m100_418M-ct2-int8"
TRANSLATION_REVISION = "7c1b2620a4e58dacecbd8bf89cfd6da7eb9eb7b0"
TRANSLATION_FILES = ["config.json", "model.bin", "sentencepiece.bpe.model", "shared_vocabulary.json"]
TRANSLATION_HASHES = {
    "model.bin": "d6703dd9f920ff896e45c3d97b490761bed5944937b90bbe6a7245f5652542d4",
    "sentencepiece.bpe.model": "d8f7c76ed2a5e0822be39f0a4f95a55eb19c78f4593ce609e2edbc2aea4d380a",
}
