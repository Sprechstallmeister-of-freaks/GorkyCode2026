from assistant import config


def test_dotenv_loads_and_deduplicates_multiple_keys(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEYS=first-key, second-key,first-key\n"
        "DEEPSEEK_MODEL=test-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEYS", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    settings = config.from_env()

    assert settings.deepseek_keys == ("first-key", "second-key")
    assert settings.deepseek_model == "test-model"
    assert "first-key" not in repr(settings) and "second-key" not in repr(settings)


def test_environment_keys_override_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEYS=file-key\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEYS", "env-key")

    assert config.from_env().deepseek_keys == ("env-key",)


def test_single_key_settings_remain_backward_compatible(tmp_path):
    settings = config.Settings(db_path=tmp_path / "db.sqlite", deepseek_api_key="single-key")
    assert settings.deepseek_keys == ("single-key",)
