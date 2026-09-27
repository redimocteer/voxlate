from pathlib import Path

from .common import VoxlateError
from .languages import direction


class Translator:
    def __init__(self, config, *, work_dir=None):
        self.source_lang, self.target_lang = direction(config)
        self.native = None
        if config.get("model_type", "").startswith("hy_mt2"):
            from .hy_translator import HyTranslator
            self.native = HyTranslator(config, work_dir=work_dir)
            self.source_lang = config.get("source_lang", "en")
            return
        import ctranslate2
        import sentencepiece as spm
        from sacremoses import MosesPunctNormalizer

        directory = Path(config["model_path"])
        self.source_lang = config.get("source_lang", "en")
        self.multilingual = config.get("model_type") == "m2m100" or (directory / "sentencepiece.bpe.model").is_file()
        if not self.multilingual and (self.source_lang, self.target_lang) != ('en', 'zh'):
            raise VoxlateError("此旧翻译模型仅支持英→中，请准备 Hy-MT2 7B。")
        self.source = spm.SentencePieceProcessor(model_file=str(directory / ("sentencepiece.bpe.model" if self.multilingual else "source.spm")))
        self.target = self.source if self.multilingual else spm.SentencePieceProcessor(model_file=str(directory / "target.spm"))
        self.normalizer = None if self.multilingual else MosesPunctNormalizer(lang="en")
        self.model = ctranslate2.Translator(str(directory), device=config["device"],
                                            compute_type=config["compute_type"],
                                            intra_threads=config["cpu_threads"])
        self.beam = config["beam_size"]

    def translate_many(self, texts, context=None, indices=None):
        if self.native:
            return self.native.translate_many(texts, context, indices)
        tokens = [self.source.encode(self.normalizer.normalize(s) if self.normalizer else s, out_type=str) + ["</s>"]
                  for s in texts]
        if self.multilingual:
            tokens = [[f"__{self.source_lang}__", *pieces] for pieces in tokens]
        if any(len(s) > 512 for s in tokens):
            raise VoxlateError("原文片段超过 512 tokens，请在 project.json 中拆分，避免截断翻译。")
        options = {"target_prefix": [[f"__{self.target_lang}__"] for _ in texts]} if self.multilingual else {}
        results = self.model.translate_batch(tokens, beam_size=self.beam, max_batch_size=16,
                                             max_input_length=0, max_decoding_length=512, **options)
        translated = [self.target.decode([t for t in r.hypotheses[0] if t not in {"</s>", "<s>", "<pad>", f"__{self.target_lang}__"}])
                      for r in results]
        if any(not s.strip() for s in translated):
            raise VoxlateError("翻译模型返回空文本")
        return translated

    def translate(self, text, source_lang=None, target_lang=None):
        if (source_lang or self.source_lang) != self.source_lang or (target_lang or self.target_lang) != self.target_lang:
            raise VoxlateError("翻译方向与当前设置不一致。")
        return self.translate_many([text])[0]
