"""Тесты парсера пакета ФНС на текстовых слоях РЕАЛЬНЫХ справок.

Фикстуры в `fixtures/` — текст, который извлекается из настоящих PDF от разных
УФНС (Карачаево-Черкесия, Волгоград ×3, Хабаровск, Чувашия). Каждая закрывает
свою ловушку — см. docstring соответствующего теста.

Табличный слой (недвижимость / земля / транспорт) тут не проверяется — он
разбирается из таблиц PDF, для него есть команда `manage.py fns_parse <файл>`.
"""
from pathlib import Path

from django.test import SimpleTestCase

from apps.procedure import fns_parser

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return fns_parser.parse_text((FIXTURES / f"fns_{name}.txt").read_text(encoding="utf-8"))


def by_number(result: dict, number: str) -> dict:
    for acc in result["accounts"]:
        if acc["number"] == number:
            return acc
    raise AssertionError(f"Счёт {number} не найден: {[a['number'] for a in result['accounts']]}")


class KardanovTests(SimpleTestCase):
    """Полная секция + Форма 9ф; блок банка рвётся границей страницы;
    у закрытого счёта ВТБ пустой «Вид счёта»; ЭСП «225-9275181484»."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.res = load("kardanov")

    def test_subject_is_debtor(self):
        self.assertEqual(self.res["subject"], "debtor")
        self.assertEqual(self.res["subject_inn"], "091801462359")
        self.assertEqual(self.res["subject_fio"], "Карданов Артур Азаматович")
        self.assertEqual(self.res["subject_birth_date"], "1998-02-11")
        self.assertEqual(self.res["case_number"], "А25-888/2026")
        self.assertEqual(self.res["formed_at"], "2026-07-07")
        self.assertTrue(self.res["has_tax_debt"])

    def test_all_20_accounts_merged_without_dupes(self):
        # 20 счетов в полной секции; 9ф (17 открытых) — подмножество, дублей быть не должно.
        self.assertEqual(len(self.res["accounts"]), 20)
        self.assertEqual(len({a["number"] for a in self.res["accounts"]}), 20)

    def test_bank_block_split_by_page_break(self):
        # 4 счёта Сбера-Волгоград уехали на следующую страницу БЕЗ шапки банка.
        acc = by_number(self.res, "40817810911009921466")
        self.assertEqual(acc["bank_inn"], "7707083893")
        self.assertEqual(acc["bank_bik"], "041806647")
        self.assertIn("Волгоградское отделение", acc["bank_name"])

    def test_bank_name_not_glued_with_table_header(self):
        acc = by_number(self.res, "40817810160310070429")
        self.assertEqual(
            acc["bank_name"],
            'Публичное акционерное общество "Сбербанк России", Карачаево-Черкесское отделение № 8585',
        )

    def test_multiline_state_cell_is_glued_back(self):
        # «прекращено / право / использовани / я» + вид счёта на двух строках.
        acc = by_number(self.res, "225-9275181484")
        self.assertEqual(acc["state"], "revoked")
        self.assertEqual(acc["state_text"], "прекращено право использования")
        self.assertEqual(acc["account_kind"], "ЭСП, не являющееся корпоративным")
        self.assertEqual(acc["closed_date"], "2025-09-17")
        self.assertEqual(acc["bank_inn"], "7835905228")

    def test_closed_account_with_empty_kind(self):
        acc = by_number(self.res, "40817810801802142700")
        self.assertEqual(acc["state"], "closed")
        self.assertEqual(acc["closed_date"], "2025-08-10")
        self.assertEqual(acc["account_kind"], "")

    def test_granted_esp(self):
        acc = by_number(self.res, "40914810300004193153")
        self.assertEqual(acc["state"], "granted")
        self.assertEqual(acc["bank_name"], 'Общество с ограниченной ответственностью "ОЗОН Банк"')

    def test_eight_distinct_banks_by_inn(self):
        self.assertEqual(len({a["bank_inn"] for a in self.res["accounts"]}), 8)

    def test_income_certificates(self):
        self.assertEqual(len(self.res["incomes"]), 2)
        c23 = next(c for c in self.res["incomes"] if c["year"] == 2023)
        self.assertEqual(c23["agent_name"], 'ООО "ИЗОЛ"')
        self.assertEqual(c23["agent_inn"], "3443038538")
        self.assertEqual(c23["agent_kpp"], "344301001")
        self.assertEqual(c23["total_income"], "268000")
        self.assertEqual(c23["tax_withheld"], "34840")
        self.assertEqual(len(c23["rows"]), 12)
        c24 = next(c for c in self.res["incomes"] if c["year"] == 2024)
        self.assertEqual(c24["tax_base"], "188800")


class LavlinskiyTests(SimpleTestCase):
    """Субъект — СУПРУГ должника; ЭСП Вайлдберриз с ведущими нулями;
    ВТБ-счёт «открыт» с пустым видом; двухстрочное имя НКО «Единая касса»."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.res = load("lavlinskiy")

    def test_subject_is_spouse_not_debtor(self):
        self.assertEqual(self.res["subject"], "spouse")
        self.assertEqual(self.res["subject_fio"], "Лавлинский Алексей Валерьевич")
        self.assertEqual(self.res["subject_inn"], "343201879180")
        self.assertEqual(self.res["debtor_fio"], "ЛАВЛИНСКАЯ НАТАЛЬЯ ВИКТОРОВНА")
        self.assertEqual(self.res["debtor_inn"], "343903974290")

    def test_no_tax_debt(self):
        self.assertIs(self.res["has_tax_debt"], False)

    def test_accounts_count(self):
        self.assertEqual(len(self.res["accounts"]), 17)

    def test_esp_with_leading_zeros(self):
        acc = by_number(self.res, "00000000200004309644")
        self.assertEqual(acc["state"], "granted")
        self.assertEqual(acc["bank_name"], 'Общество с ограниченной ответственностью "Вайлдберриз Банк"')

    def test_two_line_bank_name(self):
        acc = by_number(self.res, "40914810477182532342")
        self.assertEqual(
            acc["bank_name"],
            'Общество с ограниченной ответственностью расчетная небанковская кредитная '
            'организация "Единая касса"',
        )

    def test_open_account_with_empty_kind(self):
        acc = by_number(self.res, "40817810010084040974")
        self.assertEqual(acc["state"], "open")
        self.assertEqual(acc["account_kind"], "")
        self.assertEqual(acc["bank_inn"], "7702070139")


class ShevchenkoTests(SimpleTestCase):
    """Полной секции счетов НЕТ — только Форма 9ф (все счета открыты);
    буквенно-цифровые ЭСП «24A-…»; валютные счета; шапка банка на стыке страниц."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.res = load("shevchenko")

    def test_all_accounts_from_9f_are_open(self):
        self.assertEqual(len(self.res["accounts"]), 14)
        self.assertTrue(all(a["state"] == "open" for a in self.res["accounts"]))

    def test_letter_esp_numbers(self):
        acc = by_number(self.res, "24A-0265256652")
        self.assertEqual(acc["account_kind"], "ЭСП, не являющееся корпоративным")
        self.assertEqual(acc["bank_inn"], "7835905228")

    def test_currency_accounts(self):
        for number in ("40817840607234002241", "40817978207234002241"):
            self.assertEqual(by_number(self.res, number)["bank_bik"], "040813713")

    def test_bank_header_across_page_break(self):
        # Имя «Акционерное общество "ТБанк"» — в конце стр. 3, реквизиты — на стр. 4.
        acc = by_number(self.res, "40817810500026989512")
        self.assertEqual(acc["bank_name"], 'Акционерное общество "ТБанк"')
        self.assertEqual(acc["bank_inn"], "7710140679")


class AntonovaTests(SimpleTestCase):
    """Секции ПРОДУБЛИРОВАНЫ в одном файле; ЭСП из 9–10 цифр (ЭЛЕКСИР, ТБанк);
    имя НКО в кавычках на двух строках."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.res = load("antonova")

    def test_duplicate_sections_deduped(self):
        numbers = [a["number"] for a in self.res["accounts"]]
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertEqual(len(numbers), 23)

    def test_short_esp_numbers(self):
        self.assertEqual(by_number(self.res, "203422738")["bank_inn"], "7729496647")
        self.assertEqual(by_number(self.res, "5377855936")["bank_inn"], "7710140679")

    def test_full_section_wins_over_9f(self):
        # Счёт есть и в 9ф (без состояния), и в полной секции (закрыт) — берём полную.
        acc = by_number(self.res, "40817810811006808080")
        self.assertEqual(acc["state"], "closed")
        self.assertEqual(acc["closed_date"], "2024-05-13")

    def test_spouse_subject(self):
        self.assertEqual(self.res["subject"], "spouse")
        self.assertEqual(self.res["subject_fio"], "Антонова Ольга Викторовна")


class SidorovaTests(SimpleTestCase):
    """Состояние «в ликвидированном банке»; вид счёта «ТЕКУЩИЙ» / «Иной счет»."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.res = load("sidorova")

    def test_liquidated_bank_state(self):
        acc = by_number(self.res, "40817810993260063583")
        self.assertEqual(acc["state"], "liq_bank")
        self.assertEqual(acc["state_text"], "в ликвидированном банке")
        self.assertEqual(acc["account_kind"], "Текущий счет")

    def test_accounts_count(self):
        self.assertEqual(len(self.res["accounts"]), 29)

    def test_psb_block_ten_accounts(self):
        psb = [a for a in self.res["accounts"] if a["bank_inn"] == "7744000912"]
        self.assertEqual(len(psb), 10)


class OsinaTests(SimpleTestCase):
    """ЭСП ЮМани из 15–16 цифр; номинальный счёт; административное правонарушение."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.res = load("osina")

    def test_yumoney_esp(self):
        for number in ("4100116270193949", "410016635229291"):
            acc = by_number(self.res, number)
            self.assertEqual(acc["state"], "revoked")
            self.assertEqual(acc["bank_inn"], "7750005725")

    def test_nominal_account(self):
        self.assertEqual(by_number(self.res, "40823810075000008504")["account_kind"], "Номинальный счет")

    def test_admin_offense(self):
        self.assertEqual(len(self.res["admin"]), 1)
        item = self.res["admin"][0]
        self.assertIn("Неповиновение законному требованию", item["title"])
        self.assertEqual(item["date"], "2025-04-14")
        self.assertIn("Статья КоАП: 19.4", item["details"])

    def test_accounts_count(self):
        self.assertEqual(len(self.res["accounts"]), 25)
