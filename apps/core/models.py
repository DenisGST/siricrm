# apps/core/models.py

from django.conf import settings
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import uuid

#from apps.crm.models import Client


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создано")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        abstract = True

class Department(TimeStampedModel):
    """Department/Team model"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, verbose_name='Название отдела')
    description = models.TextField(blank=True, verbose_name='Описание')
    manager = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_departments',
        verbose_name='Руководитель отдела'
    )
    is_active = models.BooleanField(default=True, verbose_name='Активен')

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'Отдел'
        verbose_name_plural = 'Отделы'
        ordering = ['name']

class Employee(models.Model):
    ROLE_CHOICES = [
        ("operator", "Оператор"),
        ("manager", "Менеджер"),
        ("consultant", "Консультант"),
        ("assitent_legal", "Помощник юриста"),
        ("lawyer", "Юрист"),
        ("head_dep", "Руководитель отдела"),
        ("arbitration", "Арбитражный управляющий"),
        ("agent", "Агент"),
        ("managing_partner", "Управляющий партнер"),
        ("admin", "Администратор"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="employee",
        verbose_name="Сотрудник",
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        related_name='employees',
        verbose_name='Отдел'
    )
    role = models.CharField(
        "Роль",
        max_length=20,
        choices=ROLE_CHOICES,
        default="operator",
    )
    
    is_active = models.BooleanField("Активен", default=True)
    is_online = models.BooleanField(default=False, verbose_name='Онлайн')
    joined_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата присоединения')
    dismiss_at = models.DateTimeField(auto_now_add=False,null=True, blank=True, verbose_name='Дата увольнения')

    class Meta:
        verbose_name = "Сотрудник"
        verbose_name_plural = "Сотрудники"

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.department})"



class EmployeeLog(models.Model):
    """Audit log for Employee actions"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='logs',
        verbose_name='Сотрудник'
    )
    
    ACTION_CHOICES = [
        ('login', 'Вход'),
        ('logout', 'Выход'),
        ('message_sent', 'Сообщение отправлено'),
        ('message_received', 'Сообщение получено'),
        ('client_add', 'Клиент добавлен'),
        ('client_assigned', 'Клиент назначен'),
        ('client_edit', 'Данные Клиента изменены'),
        ('client_reassigned', 'Клиент переназначен'),
        ('client_status_changed', 'Статус клиента изменен'),
        ('note_added', 'Заметка добавлена'),
        ('client_unassigned', 'Клиент разъединен'),
    ]
    action = models.CharField(
        max_length=50,
        choices=ACTION_CHOICES,
        verbose_name='Действие'
    )
    
    description = models.TextField(verbose_name='Описание')
    
    # Context
    client = models.ForeignKey(
        'crm.Client',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employee_logs',
        verbose_name='Клиент'
    )
    message = models.ForeignKey(
        'crm.Message',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='logs',
        verbose_name='Сообщение'
    )
    
    # Request metadata
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name='IP адрес')
    user_agent = models.TextField(blank=True, verbose_name='User Agent')
    
    timestamp = models.DateTimeField(auto_now_add=True, verbose_name='Время')

    def __str__(self):
        return f"{self.employee} - {self.get_action_display()} at {self.timestamp}"

    class Meta:
        verbose_name = 'Лог сотрудника'
        verbose_name_plural = 'Логи осотрудников'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['employee', 'timestamp']),
            models.Index(fields=['action', 'timestamp']),
            models.Index(fields=['client', 'timestamp']),
        ]
