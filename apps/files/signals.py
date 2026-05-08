from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="crm.Client")
def client_created(sender, instance, created, **kwargs):
    if not created:
        return
    from .folder_utils import create_default_folders
    try:
        create_default_folders(instance)
    except Exception:
        pass


@receiver(post_save, sender="crm.Service")
def service_created(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        short_name = instance.name.short_name.upper()
    except Exception:
        return
    if short_name != "БФЛ":
        return
    from .folder_utils import create_bfl_folders
    try:
        create_bfl_folders(instance.client)
    except Exception:
        pass
