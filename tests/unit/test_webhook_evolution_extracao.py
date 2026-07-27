"""Extração de conteúdo do payload da Evolution.

O nó de mídia declara o MIME em campos diferentes conforme a integração da
instância: `mime_type` na WHATSAPP-BUSINESS (Cloud API oficial, medido em
`docs/evidencias/payload-midia-cloud-api.json`) e `mimetype` no Baileys. O
template atende as duas, então as duas formas são lidas — e o default por
campo continua sendo o último recurso, não o caminho normal.
"""

from typing import Any

from whatsapp_langchain.server.routes.webhook_evolution import _extrair_conteudo


def _data(no: dict[str, Any], campo: str = "audioMessage") -> dict[str, Any]:
    return {"message": {campo: no}}


class TestMimeDoNoDeMidia:
    def test_mime_type_da_cloud_api_e_lido(self):
        _, url, mime = _extrair_conteudo(
            _data(
                {
                    "mime_type": "audio/ogg; codecs=opus",
                    "url": "https://lookaside.fbsbx.com/whatsapp_business/x",
                    "ptt": True,
                }
            )
        )

        assert mime == "audio/ogg; codecs=opus"
        assert url == "https://lookaside.fbsbx.com/whatsapp_business/x"

    def test_mimetype_do_baileys_continua_lido(self):
        _, _, mime = _extrair_conteudo(
            _data({"mimetype": "audio/ogg; codecs=opus", "url": "https://mmg.enc"})
        )

        assert mime == "audio/ogg; codecs=opus"

    def test_audio_mp4_nao_vira_ogg_por_fallback(self):
        """O default do campo só entra quando NÃO há MIME declarado."""
        _, _, mime = _extrair_conteudo(_data({"mime_type": "audio/mp4"}))

        assert mime == "audio/mp4"

    def test_imagem_png_da_cloud_api_nao_vira_jpeg(self):
        _, _, mime = _extrair_conteudo(
            _data({"mime_type": "image/png"}, campo="imageMessage")
        )

        assert mime == "image/png"

    def test_mime_type_tem_precedencia_sobre_mimetype(self):
        _, _, mime = _extrair_conteudo(
            _data({"mime_type": "audio/mp4", "mimetype": "audio/ogg"})
        )

        assert mime == "audio/mp4"

    def test_mime_type_vazio_cai_no_mimetype(self):
        _, _, mime = _extrair_conteudo(
            _data({"mime_type": "   ", "mimetype": "audio/mpeg"})
        )

        assert mime == "audio/mpeg"

    def test_sem_mime_declarado_cai_no_padrao_do_campo(self):
        _, _, mime = _extrair_conteudo(_data({"seconds": 3}))

        assert mime == "audio/ogg"

    def test_mime_nao_string_cai_no_padrao_do_campo(self):
        _, _, mime = _extrair_conteudo(_data({"mime_type": {"x": 1}}))

        assert mime == "audio/ogg"

    def test_legenda_da_imagem_vira_texto(self):
        texto, url, mime = _extrair_conteudo(
            _data(
                {
                    "mime_type": "image/jpeg",
                    "caption": "olha isso",
                    "url": "https://lookaside.fbsbx.com/whatsapp_business/y",
                },
                campo="imageMessage",
            )
        )

        assert texto == "olha isso"
        assert mime == "image/jpeg"
        assert url == "https://lookaside.fbsbx.com/whatsapp_business/y"
