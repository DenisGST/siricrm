import re

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render, redirect
from django.views.decorators.http import require_POST

from apps.core.models import Employee
from apps.crm.models import Service, Region, LegalEntity, LegalEntityKind, Client
from .models import (
    QuestionnaireTemplate, QuestionnairePage, Question, QuestionChoice,
    QuestionnaireResponse, Answer, QUESTION_TYPES,
)


def _current_employee(request):
    try:
        return Employee.objects.get(user=request.user)
    except Employee.DoesNotExist:
        return None


def _log_questionnaire_event(event_type, response, employee):
    from apps.crm.models import ClientEvent
    label = response.template.title
    svc   = response.service
    ClientEvent.objects.create(
        client=svc.client,
        event_type=event_type,
        description=f"{label} · {svc.name.short_name}" + (f" № {svc.numb_dogovor}" if svc.numb_dogovor else ""),
        employee=employee,
    )


# ─── Справочник ────────────────────────────────────────

@login_required
def template_list(request):
    from apps.crm.models import ServiceName
    templates = QuestionnaireTemplate.objects.select_related("service_name").all()
    service_names_without = ServiceName.objects.filter(
        is_active=True, questionnaire_template__isnull=True,
    ).order_by("short_name")
    return render(request, "questionnaire/admin/template_list.html", {
        "templates": templates,
        "service_names_without": service_names_without,
    })


@login_required
def template_detail(request, pk):
    tmpl  = get_object_or_404(QuestionnaireTemplate, pk=pk)
    pages = tmpl.pages.prefetch_related("questions__choices").all()
    return render(request, "questionnaire/admin/template_detail.html", {
        "tmpl": tmpl, "pages": pages, "question_types": QUESTION_TYPES,
    })


@login_required
def template_create(request):
    from apps.crm.models import ServiceName
    if request.method == "POST":
        sn    = get_object_or_404(ServiceName, pk=request.POST.get("service_name"))
        title = (request.POST.get("title") or f"Анкета {sn.short_name}").strip()
        QuestionnaireTemplate.objects.create(service_name=sn, title=title)
        resp = HttpResponse("")
        resp["HX-Trigger"] = "reloadTemplates"
        return resp
    return HttpResponseBadRequest()


@login_required
def template_toggle(request, pk):
    tmpl = get_object_or_404(QuestionnaireTemplate, pk=pk)
    tmpl.is_active = not tmpl.is_active
    tmpl.save(update_fields=["is_active", "updated_at"])
    resp = HttpResponse("")
    resp["HX-Trigger"] = "reloadTemplates"
    return resp


@login_required
def page_add(request, tmpl_pk):
    tmpl = get_object_or_404(QuestionnaireTemplate, pk=tmpl_pk)
    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()
        QuestionnairePage.objects.create(template=tmpl, title=title, order=tmpl.pages.count())
        resp = HttpResponse("")
        resp["HX-Trigger"] = "reloadTemplate"
        return resp
    return render(request, "questionnaire/admin/page_form.html", {"tmpl": tmpl})


@login_required
def page_delete(request, pk):
    get_object_or_404(QuestionnairePage, pk=pk).delete()
    resp = HttpResponse("")
    resp["HX-Trigger"] = "reloadTemplate"
    return resp


@login_required
def question_form(request, page_pk, q_pk=None):
    from apps.crm.models import LegalEntityKind
    page = get_object_or_404(QuestionnairePage, pk=page_pk)
    q    = get_object_or_404(Question, pk=q_pk) if q_pk else None

    if request.method == "POST":
        data = request.POST
        if not q:
            q = Question(page=page, order=page.questions.count())
        q.text              = (data.get("text") or "").strip()
        q.hint              = (data.get("hint") or "").strip()
        q.question_type     = data.get("question_type") or "text"
        q.is_required       = bool(data.get("is_required"))
        q.allow_custom_text = bool(data.get("allow_custom_text"))
        le_kind_id          = data.get("legal_entity_kind") or None
        q.legal_entity_kind = LegalEntityKind.objects.filter(pk=le_kind_id).first() if le_kind_id else None
        show_if_q_id        = data.get("show_if_question") or None
        q.show_if_question  = Question.objects.filter(pk=show_if_q_id).first() if show_if_q_id else None
        q.show_if_value     = (data.get("show_if_value") or "").strip()
        q.save()

        if q.question_type in ("choice", "multi_choice"):
            q.choices.all().delete()
            for i, txt in enumerate(data.getlist("choice_text")):
                txt = txt.strip()
                if txt:
                    extras      = data.getlist("choice_extra")
                    extra_hints = data.getlist("choice_extra_hint")
                    QuestionChoice.objects.create(
                        question=q, text=txt, order=i,
                        has_extra_field=bool(extras[i] if i < len(extras) else False),
                        extra_field_hint=(extra_hints[i].strip() if i < len(extra_hints) else ""),
                    )
        resp = HttpResponse("")
        resp["HX-Trigger"] = "reloadTemplate"
        return resp

    le_kinds       = LegalEntityKind.objects.order_by("name")
    page_questions = page.questions.exclude(pk=q_pk) if q_pk else page.questions.all()
    return render(request, "questionnaire/admin/question_form.html", {
        "page": page, "q": q,
        "question_types": QUESTION_TYPES,
        "le_kinds": le_kinds,
        "page_questions": page_questions,
    })


@login_required
def question_delete(request, pk):
    get_object_or_404(Question, pk=pk).delete()
    resp = HttpResponse("")
    resp["HX-Trigger"] = "reloadTemplate"
    return resp


# ─── Квиз ──────────────────────────────────────────────

@login_required
@login_required
def client_responses(request, client_pk):
    client = get_object_or_404(Client, pk=client_pk)
    responses = (
        QuestionnaireResponse.objects
        .filter(service__client=client)
        .select_related("template", "filled_by__user", "service__name")
        .order_by("-updated_at")
    )
    # Услуги клиента у которых есть шаблон анкеты
    services_with_tmpl = (
        Service.objects
        .filter(client=client)
        .select_related("name__questionnaire_template")
        .filter(name__questionnaire_template__isnull=False, name__questionnaire_template__is_active=True)
    )
    return render(request, "questionnaire/client_response_list.html", {
        "client": client,
        "responses": responses,
        "services_with_tmpl": services_with_tmpl,
    })


@login_required
def service_responses(request, service_pk):
    service = get_object_or_404(Service, pk=service_pk)
    responses = (
        QuestionnaireResponse.objects
        .filter(service=service)
        .select_related("template", "filled_by__user")
        .order_by("-updated_at")
    )
    return render(request, "questionnaire/response_list.html", {
        "service": service,
        "responses": responses,
    })


@login_required
@require_POST
def response_delete(request, pk):
    resp_obj = get_object_or_404(QuestionnaireResponse, pk=pk)
    service = resp_obj.service
    client  = service.client
    emp = _current_employee(request)
    _log_questionnaire_event("questionnaire_deleted", resp_obj, emp)
    resp_obj.delete()
    # Если пришли из клиентского списка — возвращаем туда
    if request.GET.get("from") == "client":
        responses = (
            QuestionnaireResponse.objects
            .filter(service__client=client)
            .select_related("template", "filled_by__user", "service__name")
            .order_by("-updated_at")
        )
        services_with_tmpl = (
            Service.objects
            .filter(client=client)
            .select_related("name__questionnaire_template")
            .filter(name__questionnaire_template__isnull=False, name__questionnaire_template__is_active=True)
        )
        return render(request, "questionnaire/client_response_list.html", {
            "client": client, "responses": responses, "services_with_tmpl": services_with_tmpl,
        })
    responses = (
        QuestionnaireResponse.objects
        .filter(service=service)
        .select_related("template", "filled_by__user")
        .order_by("-updated_at")
    )
    return render(request, "questionnaire/response_list.html", {
        "service": service,
        "responses": responses,
    })


def quiz_start(request, service_pk):
    service = get_object_or_404(Service, pk=service_pk)
    emp = _current_employee(request)
    try:
        tmpl = service.name.questionnaire_template
    except QuestionnaireTemplate.DoesNotExist:
        return HttpResponse("Анкета для этой услуги не настроена", status=404)
    response = QuestionnaireResponse.objects.create(
        service=service, template=tmpl, filled_by=emp,
    )
    _log_questionnaire_event("questionnaire_created", response, emp)
    return quiz_page(request, response.pk, 0)


@login_required
def quiz_page(request, pk, page_num):
    response = get_object_or_404(QuestionnaireResponse, pk=pk)
    pages    = list(response.template.pages.prefetch_related(
        "questions__choices", "questions__sub_questions__choices",
    ).all())
    total = len(pages)

    if total == 0:
        return redirect("questionnaire:quiz_complete", pk=pk)
    if page_num >= total:
        return redirect("questionnaire:quiz_complete", pk=pk)

    page         = pages[page_num]
    question_ids = list(page.questions.values_list("pk", flat=True))
    answers      = {
        str(a.question_id): a.value
        for a in Answer.objects.filter(response=response, question_id__in=question_ids, group_index=0)
    }

    from apps.questionnaire.models import QuestionChoice
    from apps.core.models import Department

    has_region_ref   = page.questions.filter(question_type="region_ref").exists()
    has_le_ref       = page.questions.filter(question_type="legal_entity_ref").exists()
    # client_ref/employee_ref как самостоятельный тип вопроса — рендерят select, нужен список
    # choice extras с этими типами используют живой поиск → список не нужен
    has_client_ref   = page.questions.filter(question_type="client_ref").exists()
    has_employee_ref = page.questions.filter(question_type="employee_ref").exists()

    le_kind_ids    = list(page.questions.filter(question_type="legal_entity_ref").values_list("legal_entity_kind_id", flat=True))
    legal_entities = LegalEntity.objects.filter(kind_id__in=le_kind_ids).order_by("name") if has_le_ref and le_kind_ids else (
                     LegalEntity.objects.order_by("name") if has_le_ref else [])

    ctx = {
        "response":      response,
        "page":          page,
        "page_num":      page_num,
        "total_pages":   total,
        "page_range":    range(total),
        "answers":       answers,
        "regions":       Region.objects.order_by("name") if has_region_ref else [],
        "legal_entities":legal_entities,
        "clients":       Client.objects.order_by("last_name", "first_name")[:300] if has_client_ref else [],
        "employees":     Employee.objects.filter(is_active=True).select_related("user").order_by("user__last_name") if has_employee_ref else [],
        "prev_num":      page_num - 1 if page_num > 0 else None,
        "next_num":      page_num + 1 if page_num < total - 1 else None,
        "is_last":       page_num == total - 1,
    }
    return render(request, "questionnaire/quiz/step.html", ctx)


@login_required
@require_POST
def quiz_save_page(request, pk, page_num):
    response = get_object_or_404(QuestionnaireResponse, pk=pk)
    pages    = list(response.template.pages.prefetch_related("questions").all())
    if page_num >= len(pages):
        return HttpResponseBadRequest()
    for q in pages[page_num].questions.all():
        Answer.objects.update_or_create(
            response=response, question=q, group_index=0,
            defaults={"value": _extract_answer(request.POST, q)},
        )
    next_num = page_num + 1
    if next_num >= len(pages):
        response.is_complete  = True
        response.current_page = page_num
    else:
        response.is_complete  = False
        response.current_page = next_num
    response.save(update_fields=["current_page", "is_complete", "updated_at"])
    emp = _current_employee(request)
    _log_questionnaire_event("questionnaire_edited", response, emp)

    if response.is_complete:
        return quiz_complete(request, pk)
    return quiz_page(request, pk, next_num)


def _extract_answer(post, q):
    key = f"q_{q.pk}"
    qt  = q.question_type
    if qt in ("text", "textarea", "number", "money", "date"):
        return {"v": (post.get(key) or "").strip()}
    if qt == "yes_no":
        return {"v": post.get(key) or ""}
    if qt == "choice":
        selected = post.get(key) or ""
        # Доп. поля могут быть per-choice (key_extra_<choice_pk>) или общим (key_extra)
        extras = {}
        for k, v in post.items():
            if k.startswith(f"{key}_extra_") and v:
                extras[k[len(f"{key}_extra_"):]] = v.strip()
        extra_simple = (post.get(f"{key}_extra") or "").strip()
        return {"v": selected, "extra": extra_simple, "extras": extras}
    if qt == "multi_choice":
        vals         = post.getlist(key)
        extras       = {v: (post.get(f"{key}_{v}_extra") or "").strip() for v in vals if post.get(f"{key}_{v}_extra")}
        extras_amount= {v: (post.get(f"{key}_{v}_extra_amount") or "").strip() for v in vals if post.get(f"{key}_{v}_extra_amount")}
        return {"v": vals, "extras": extras, "extras_amount": extras_amount, "comment": (post.get(f"{key}_comment") or "").strip()}
    if qt == "full_name_date":
        return {"fio": (post.get(f"{key}_fio") or "").strip(), "dob": (post.get(f"{key}_dob") or "").strip()}
    if qt in ("region_ref", "legal_entity_ref", "client_ref", "employee_ref"):
        return {"ref": (post.get(key) or "").strip(), "text": (post.get(f"{key}_text") or "").strip()}
    if qt == "marital_status":
        marital  = post.get(f"{key}_marital") or "never"
        divorced = bool(post.get(f"{key}_divorced"))
        cs = {}
        if marital == "married":
            cs = {
                "client_id":    (post.get(f"{key}_cs_client_id")    or "").strip(),
                "name":         (post.get(f"{key}_cs_name")         or "").strip(),
                "marriage_date":(post.get(f"{key}_cs_marriage_date")or "").strip(),
            }
        indices = set()
        for k in post:
            m = re.match(rf'^{re.escape(key)}_d(\d+)_', k)
            if m:
                indices.add(int(m.group(1)))
        divorces = []
        for i in sorted(indices):
            d = {
                "client_id":    (post.get(f"{key}_d{i}_client_id")    or "").strip(),
                "name":         (post.get(f"{key}_d{i}_name")         or "").strip(),
                "marriage_date":(post.get(f"{key}_d{i}_marriage_date")or "").strip(),
                "divorce_date": (post.get(f"{key}_d{i}_divorce_date") or "").strip(),
            }
            divorces.append(d)
        return {"marital": marital, "divorced": divorced, "current_spouse": cs, "divorces": divorces}
    if qt == "bank_debts":
        indices = set()
        for k in post:
            m = re.match(rf'^{re.escape(key)}_b(\d+)_', k)
            if m:
                indices.add(int(m.group(1)))
        entries = []
        for i in sorted(indices):
            p = f"{key}_b{i}_"
            entries.append({f: (post.get(f"{p}{f}") or "").strip() for f in [
                "bank_id", "bank_name", "loan_amount", "balance",
                "date_taken", "last_payment_date", "loan_type",
                "overdue", "court_decision", "enforcement",
                "collectors", "collectors_name", "comment",
            ]})
        return {"entries": entries}
    if qt == "mfo_debts":
        mode = post.get(f"{key}_mode") or "unknown"
        if mode == "known":
            indices = set()
            for k in post:
                m2 = re.match(rf"^{re.escape(key)}_m(\d+)_", k)
                if m2:
                    indices.add(int(m2.group(1)))
            entries = []
            for i in sorted(indices):
                p = f"{key}_m{i}_"
                entries.append({f: (post.get(f"{p}{f}") or "").strip() for f in [
                    "mfo_id", "mfo_name", "loan_amount", "balance",
                    "date_taken", "last_payment_date", "overdue",
                    "court_decision", "collectors", "collectors_name", "comment",
                ]})
            return {"mode": "known", "entries": entries}
        return {
            "mode": "unknown",
            "count":        (post.get(f"{key}_unknown_count")  or "").strip(),
            "total_amount": (post.get(f"{key}_unknown_amount") or "").strip(),
        }
    if qt == "property_assets":
        has_assets = post.get(f"{key}_has") or "no"
        indices = set()
        for k in post:
            m = re.match(rf"^{re.escape(key)}_p(\d+)_", k)
            if m:
                indices.add(int(m.group(1)))
        entries = []
        for i in sorted(indices):
            p = f"{key}_p{i}_"
            entries.append({f: (post.get(f"{p}{f}") or "").strip() for f in [
                "asset_type", "name", "acquisition", "value",
                "pledged", "in_marriage", "auction", "comment",
            ]})
        return {"has_assets": has_assets, "entries": entries}
    if qt == "utility_debts":
        indices = set()
        for k in post:
            m = re.match(rf"^{re.escape(key)}_u(\d+)_", k)
            if m:
                indices.add(int(m.group(1)))
        entries = []
        for i in sorted(indices):
            p = f"{key}_u{i}_"
            entries.append({
                "org_name":  (post.get(f"{p}org_name")  or "").strip(),
                "debt_name": (post.get(f"{p}debt_name") or "").strip(),
                "amount":    (post.get(f"{p}amount")    or "").strip(),
            })
        return {"entries": entries}
    if qt == "fine_debts":
        indices = set()
        for k in post:
            m = re.match(rf"^{re.escape(key)}_f(\d+)_", k)
            if m:
                indices.add(int(m.group(1)))
        entries = []
        for i in sorted(indices):
            p = f"{key}_f{i}_"
            entries.append({
                "agency": (post.get(f"{p}agency") or "").strip(),
                "reason": (post.get(f"{p}reason") or "").strip(),
                "amount": (post.get(f"{p}amount") or "").strip(),
            })
        return {"entries": entries}
    if qt == "court_debts":
        indices = set()
        for k in post:
            m = re.match(rf"^{re.escape(key)}_c(\d+)_", k)
            if m:
                indices.add(int(m.group(1)))
        entries = []
        for i in sorted(indices):
            p = f"{key}_c{i}_"
            entries.append({
                "court_name": (post.get(f"{p}court_name") or "").strip(),
                "decision":   (post.get(f"{p}decision")   or "").strip(),
                "amount":     (post.get(f"{p}amount")     or "").strip(),
            })
        return {"entries": entries}
    if qt == "other_debts":
        indices = set()
        for k in post:
            m = re.match(rf"^{re.escape(key)}_o(\d+)_", k)
            if m:
                indices.add(int(m.group(1)))
        entries = []
        for i in sorted(indices):
            p = f"{key}_o{i}_"
            entries.append({
                "essence": (post.get(f"{p}essence") or "").strip(),
                "amount":  (post.get(f"{p}amount")  or "").strip(),
            })
        return {"entries": entries}
    if qt == "sold_assets":
        has_sold = post.get(f"{key}_has") or "no"
        indices = set()
        for k in post:
            m = re.match(rf"^{re.escape(key)}_s(\d+)_", k)
            if m:
                indices.add(int(m.group(1)))
        entries = []
        for i in sorted(indices):
            p = f"{key}_s{i}_"
            entries.append({f: (post.get(f"{p}{f}") or "").strip() for f in [
                "asset_type", "name", "sale_type", "value",
                "buyer_type", "has_docs", "strategy", "comment",
            ]})
        return {"has_sold": has_sold, "entries": entries}
    if qt == "tax_debts":
        has_debt = post.get(f"{key}_has") or "no"
        tax_keys = ["commercial", "property_realty", "property_car", "ndfl", "other"]
        types = [k for k in tax_keys if post.get(f"{key}_t_{k}")]
        result = {
            "has_debt":     has_debt,
            "types":        types,
            "other_name":   (post.get(f"{key}_other_name")   or "").strip(),
            "other_amount": (post.get(f"{key}_other_amount") or "").strip(),
        }
        for k in ["commercial", "property_realty", "property_car", "ndfl"]:
            result[f"a_{k}"] = (post.get(f"{key}_a_{k}") or "").strip()
        return result
    if qt == "children_list":
        has_children = post.get(f"{key}_has") or "no"
        indices = set()
        for k in post:
            m = re.match(rf"^{re.escape(key)}_ch(\d+)_", k)
            if m:
                indices.add(int(m.group(1)))
        entries = []
        for i in sorted(indices):
            entries.append({
                "fio": (post.get(f"{key}_ch{i}_fio") or "").strip(),
                "dob": (post.get(f"{key}_ch{i}_dob") or "").strip(),
            })
        return {"has_children": has_children, "entries": entries}
    return {"v": (post.get(key) or "")}


@login_required
def ref_search(request):
    from django.db.models import Q
    from apps.core.models import Department
    q       = (request.GET.get("q") or "").strip()
    rtype   = request.GET.get("type") or "client"
    results = []
    if len(q) >= 2:
        if rtype == "client":
            items = Client.objects.filter(
                Q(last_name__icontains=q) | Q(first_name__icontains=q) | Q(phone__icontains=q)
            ).order_by("last_name", "first_name")[:15]
            results = [{"id": str(i.pk), "label": f"{i.last_name} {i.first_name}{' (' + i.phone + ')' if i.phone else ''}"} for i in items]
        elif rtype == "employee":
            items = Employee.objects.filter(
                is_active=True,
            ).filter(
                Q(user__last_name__icontains=q) | Q(user__first_name__icontains=q)
            ).select_related("user").order_by("user__last_name")[:15]
            results = [{"id": str(i.pk), "label": f"{i.user.last_name} {i.user.first_name}"} for i in items]
        elif rtype == "agent":
            dept = Department.objects.filter(name__icontains="агент").first()
            qs   = Employee.objects.filter(is_active=True)
            if dept:
                qs = qs.filter(department=dept)
            items = qs.filter(
                Q(user__last_name__icontains=q) | Q(user__first_name__icontains=q)
            ).select_related("user").order_by("user__last_name")[:15]
            results = [{"id": str(i.pk), "label": f"{i.user.last_name} {i.user.first_name}"} for i in items]
        elif rtype == "bank":
            bank_kind = LegalEntityKind.objects.filter(name__icontains="банк").first()
            qs = LegalEntity.objects.filter(kind=bank_kind) if bank_kind else LegalEntity.objects.all()
            items = qs.filter(name__icontains=q).order_by("name")[:15]
            results = [{"id": str(i.pk), "label": i.name} for i in items]
        elif rtype == "mfo":
            mfo_kind = LegalEntityKind.objects.filter(name__icontains="микро").first()
            qs = LegalEntity.objects.filter(kind=mfo_kind) if mfo_kind else LegalEntity.objects.all()
            items = qs.filter(name__icontains=q).order_by("name")[:15]
            results = [{"id": str(i.pk), "label": i.name} for i in items]
    from django.http import JsonResponse
    return JsonResponse({"results": results})


@login_required
@require_POST
def quick_add_client(request):
    from django.http import JsonResponse
    last_name  = (request.POST.get("last_name")  or "").strip()
    first_name = (request.POST.get("first_name") or "").strip()
    if not last_name:
        return JsonResponse({"error": "Укажите фамилию"}, status=400)
    client = Client.objects.create(last_name=last_name, first_name=first_name)
    label  = f"{client.last_name} {client.first_name}".strip()
    return JsonResponse({"id": str(client.pk), "label": label})


@login_required
def quiz_complete(request, pk):
    response  = get_object_or_404(QuestionnaireResponse, pk=pk)
    pages     = list(response.template.pages.prefetch_related("questions__choices").all())
    answers   = {str(a.question_id): a.value for a in Answer.objects.filter(response=response)}
    responses = (
        QuestionnaireResponse.objects
        .filter(service=response.service)
        .select_related("template", "filled_by__user")
        .order_by("-updated_at")
    )
    ctx = {
        "response": response, "pages": pages, "answers": answers,
        "service": response.service, "responses": responses,
    }
    return render(request, "questionnaire/quiz/complete.html", ctx)


@login_required
def download_pdf(request, pk):
    from django.http import HttpResponse
    from .pdf import generate_response_pdf, upload_pdf_async
    response = get_object_or_404(QuestionnaireResponse, pk=pk)
    try:
        pdf_bytes = generate_response_pdf(response)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("PDF generation failed: %s", e)
        return HttpResponse("Ошибка при генерации PDF.", status=500)
    # Фоновая загрузка в S3 через Celery
    upload_pdf_async.delay(str(response.pk))
    client = response.service.client
    filename = f"anketa_{client.last_name}_{client.first_name}.pdf".replace(" ", "_")
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
