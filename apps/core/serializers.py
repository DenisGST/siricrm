from rest_framework import serializers
from apps.core.models import *

class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = '__all__'

class EmployeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = '__all__'

class EmployeeLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmployeeLog
        fields = '__all__'