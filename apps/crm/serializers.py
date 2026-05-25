from rest_framework import serializers
from apps.crm.models import *


class ClientPhoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientPhone
        fields = ('id', 'phone', 'purpose', 'is_active')


class ClientSerializer(serializers.ModelSerializer):
    phones = ClientPhoneSerializer(many=True, read_only=True)

    class Meta:
        model = Client
        fields = '__all__'

class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = '__all__'

