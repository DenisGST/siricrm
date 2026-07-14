"""Автоопределение адресата запроса в госорган.

По типу запроса (`RequestType.recipient_lookup` + `recipient_kind`), региону/
адресу клиента и запомненным правилам (`RecipientRule`) подбираем `LegalEntity`
получателя. Плейсхолдеры `{Адресат}`/`{Адрес}` документа уже берутся из
`Request.recipient` — эта логика лишь выбирает, кого туда поставить.

Реальность справочника: однозначно «один орган на регион» получается только
для видов регионального уровня (ГИМС, ОСФР, Гостехнадзор). МРЭО/ФНС/ЗАГС/суды —
районного уровня (на регион десятки-сотни органов), поэтому:
  • ФНС — точная инспекция по коду ИФНС из адреса клиента (`Address.tax_office`);
  • остальные районные — гибрид: сузить кандидатов по району/городу клиента,
    иначе вернуть отфильтрованный по региону короткий список для ручного выбора;
  • запомненный `RecipientRule` (ручной выбор) имеет приоритет над подбором.
"""
from __future__ import annotations

from typing import Optional

from apps.crm.models import LegalEntity, Region


# Служебные слова в названиях районов/городов — выкидываем при сравнении.
_TYPE_WORDS = {
    "город", "г", "гор", "район", "р-н", "рн", "округ", "городской", "сельский",
    "муниципальный", "пгт", "рп", "село", "с", "деревня", "д", "поселок",
    "посёлок", "станица", "ст", "обл", "область", "край", "республика", "респ",
    "ао", "им", "имени",
}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace("ё", "е").split())


def _pick_address(client):
    """Адрес для определения региона/района: регистрация → факт → почтовый → любой."""
    if not client:
        return None
    addrs = list(client.addresses.all())
    if not addrs:
        return None
    by_type = {a.address_type: a for a in addrs}
    for t in ("registration", "actual", "postal", "default", "other"):
        if by_type.get(t):
            return by_type[t]
    return addrs[0]


def client_region(client, service=None) -> Optional[Region]:
    """Регион проживания: из адреса (КЛАДР[:2] → Region.number), фолбэк Service.region."""
    addr = _pick_address(client)
    if addr and addr.region_kladr_id and len(addr.region_kladr_id) >= 2:
        try:
            num = int(addr.region_kladr_id[:2])
        except (TypeError, ValueError):
            num = None
        if num:
            r = Region.objects.filter(number=num).first()
            if r:
                return r
    if service is not None and getattr(service, "region_id", None):
        return service.region
    return None


def client_locality(client) -> str:
    """Район/город клиента для сужения районных органов (сырой текст, без нормализации)."""
    addr = _pick_address(client)
    if not addr:
        return ""
    for f in (addr.city_district_with_type, addr.city_with_type, addr.city,
              addr.area_with_type, addr.area, addr.settlement_with_type,
              addr.settlement):
        if f and f.strip():
            return f.strip()
    return ""


def locality_key(client) -> str:
    """Нормализованный ключ района/города клиента (для RecipientRule.district)."""
    return _norm(client_locality(client))


def _stem(word: str) -> str:
    """Грубый стем: отбрасываем падежное окончание, чтобы «кировский» ловил
    «кировского района», «центральный» — «центрального». Для коротких слов
    возвращаем как есть."""
    return word[: max(4, len(word) - 2)] if len(word) >= 6 else word


def _core_tokens(locality: str) -> list[str]:
    """Значимые стемы названия района/города (без «город»/«район»/…)."""
    raw = _norm(locality).replace(".", " ").replace("-", " ")
    return [_stem(w) for w in raw.split()
            if w not in _TYPE_WORDS and len(w) > 2]


def _locality_match(le: LegalEntity, tokens: list[str]) -> bool:
    if not tokens:
        return False
    hay = _norm(" ".join([
        le.name or "", le.short_name or "",
        le.legal_address or "", le.actual_address or "", le.postal_address or "",
    ]))
    return any(t in hay for t in tokens)


def remembered_recipient(kind, region, locality):
    """Запомненное правило: точное (регион+район) → правило на весь регион."""
    from .models import RecipientRule
    if not (kind and region):
        return None
    d = _norm(locality)
    rule = (RecipientRule.objects
            .filter(kind=kind, region=region, district=d)
            .select_related("recipient").first())
    if not rule and d:
        rule = (RecipientRule.objects
                .filter(kind=kind, region=region, district="")
                .select_related("recipient").first())
    return rule.recipient if rule else None


def resolve_recipient(request_type, client, service=None):
    """Подобрать адресата запроса.

    Возвращает dict:
      recipient   — LegalEntity | None (авто-подобранный, можно ставить сразу)
      candidates  — list[LegalEntity] (отфильтрованный список для ручного выбора)
      reason      — код причины (для подсказки в UI)
      region      — Region | None
      locality    — str (район/город клиента)
    """
    lookup = getattr(request_type, "recipient_lookup", RequestTypeLookup.MANUAL)
    kind = getattr(request_type, "recipient_kind", None)
    region = client_region(client, service)
    locality = client_locality(client)

    def out(recipient, candidates, reason):
        return {
            "recipient": recipient, "candidates": candidates,
            "reason": reason, "region": region, "locality": locality,
        }

    if lookup == RequestTypeLookup.NONE:
        return out(None, [], "none")

    # Уведомления ФУ: адресат — не госорган, а сам должник или его кредиторы.
    # Госорган не подбираем; кого ставить — решают services (ФИО должника /
    # разворот в письмо на каждого кредитора из анкеты).
    if lookup == RequestTypeLookup.DEBTOR:
        return out(None, [], "debtor")
    if lookup == RequestTypeLookup.CREDITORS:
        return out(None, [], "creditors")

    # ФНС — точная инспекция по коду ИФНС из адреса клиента.
    if lookup == RequestTypeLookup.FNS and kind:
        addr = _pick_address(client)
        code = ((addr.tax_office or "").strip() if addr else "")
        if code:
            fns = (LegalEntity.objects
                   .filter(kind=kind, ifns_code=code, is_active=True)
                   .order_by("name").first())
            if fns:
                return out(fns, [fns], "fns_code")
        # код не нашли → падаем в подбор по региону ниже

    # Запомненный ручной выбор — приоритет над подбором.
    if kind and region:
        rem = remembered_recipient(kind, region, locality)
        if rem:
            return out(rem, [rem], "remembered")

    if lookup == RequestTypeLookup.MANUAL or not kind:
        return out(None, [], "manual")

    # lookup == region (или ФНС-фолбэк): фильтр по виду + региону.
    if not region:
        return out(None, [], "no_region")
    qs = (LegalEntity.objects
          .filter(kind=kind, region=region, is_active=True)
          .select_related("region").order_by("name"))
    cands = list(qs[:60])
    if len(cands) == 1:
        return out(cands[0], cands, "region_unique")

    # Гибрид: сузить по району/городу клиента.
    if locality and len(cands) > 1:
        tokens = _core_tokens(locality)
        narrowed = [c for c in cands if _locality_match(c, tokens)]
        if len(narrowed) == 1:
            return out(narrowed[0], narrowed, "district_unique")
        if narrowed:
            return out(None, narrowed, "district_many")

    return out(None, cands, "region_many")


class RequestTypeLookup:
    """Дублирует значения RequestType.LOOKUP_* без импорта моделей на уровне модуля."""
    NONE = "none"
    REGION = "region"
    FNS = "fns_by_address"
    MANUAL = "manual"
    DEBTOR = "debtor"
    CREDITORS = "creditors"
