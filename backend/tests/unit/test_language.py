from app.resumes.language import is_predominantly_english

_ENGLISH_RESUME = """Jane Doe
Senior Backend Engineer

Summary
Backend engineer with eight years of experience designing and building
distributed systems. Passionate about clean architecture, automated testing
and continuous delivery.

Experience
Led the migration of a legacy monolith to microservices running on Kubernetes,
reducing deployment time from hours to minutes. Designed public REST APIs used
by millions of customers every day and mentored a team of five engineers.

Education
Bachelor of Science in Computer Science, State University.

Skills
Python, Java, PostgreSQL, Docker, cloud infrastructure, observability.
"""

_PORTUGUESE_RESUME = """Maria Silva
Engenheira de Software Sênior

Resumo
Engenheira de software com oito anos de experiência no desenvolvimento de
sistemas distribuídos. Apaixonada por arquitetura limpa, testes automatizados
e entrega contínua de valor para os clientes.

Experiência
Liderou a migração de um sistema legado para microsserviços executando em
nuvem, reduzindo o tempo de implantação de horas para minutos. Projetou APIs
utilizadas por milhões de clientes todos os dias e orientou uma equipe de
cinco pessoas.

Formação
Bacharelado em Ciência da Computação pela Universidade Federal.

Habilidades
Desenvolvimento de sistemas, bancos de dados relacionais, conteinerização e
observabilidade de aplicações em produção.
"""


def test_cv_14_english_resume_is_predominantly_english() -> None:
    assert is_predominantly_english(_ENGLISH_RESUME) is True


def test_cv_14_portuguese_resume_is_not_predominantly_english() -> None:
    assert is_predominantly_english(_PORTUGUESE_RESUME) is False


def test_cv_14_mostly_portuguese_with_english_section_is_not_english() -> None:
    mixed = _PORTUGUESE_RESUME + "\n\nSkills\nPython, Java and cloud infrastructure.\n"
    assert is_predominantly_english(mixed) is False


def test_cv_14_detection_is_deterministic() -> None:
    results = {is_predominantly_english(_ENGLISH_RESUME) for _ in range(5)}
    assert results == {True}


def test_cv_14_empty_or_symbol_only_text_is_not_english() -> None:
    assert is_predominantly_english("") is False
    assert is_predominantly_english("  \n 12345 --- ### \n") is False
