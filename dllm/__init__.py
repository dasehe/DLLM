# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
__version__ = "0.7.0"

# Avisos legales apropiados (GNU AGPL v3, secciones 5.d, 7.b y 13). Ver NOTICE.
# Quien distribuya una versión modificada debe conservar ATTRIBUTION y apuntar SOURCE_URL a su propio código.
ATTRIBUTION = "DLLM — idea original y desarrollo inicial de David Serrano Hernando"
COPYRIGHT = "Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM"
LICENSE_ID = "AGPL-3.0-or-later"
SOURCE_URL = "https://github.com/dasehe/dllm"


def legal_notice():
    return (f"{ATTRIBUTION}\n{COPYRIGHT}\nSoftware libre bajo licencia GNU {LICENSE_ID}, SIN NINGUNA GARANTÍA.\n"
            f"Código fuente: {SOURCE_URL}")
