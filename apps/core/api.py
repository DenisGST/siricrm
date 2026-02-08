from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.db.models import Count

from apps.core.models import (
    Employee, EmployeeLog, Department
)
from apps.core.serializers import (
    EmployeeSerializer,
    EmployeeLogSerializer,
    DepartmentSerializer,
)

from .models import Employee
from apps.crm.models import Client, Message

class DepartmentViewSet(viewsets.ModelViewSet):
    """
    API ViewSet for Department management
    
    Endpoints:
    - GET /api/departments/ - List all departments
    - POST /api/departments/ - Create new department
    - GET /api/departments/{id}/ - Get department details
    - PUT /api/departments/{id}/ - Update department
    - DELETE /api/departments/{id}/ - Delete department
    - GET /api/departments/{id}/employees/ - Get employees in department
    """
    queryset = Department.objects.prefetch_related('employees', 'manager')
    serializer_class = DepartmentSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'created_at']
    
    @action(detail=True, methods=['get'])
    def employees(self, request, pk=None):
        """Get all employees in this department"""
        department = self.get_object()
        employees = department.employee.filter(is_active=True)
        serializer = EmployeeSerializer(employees, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """Get department statistics"""
        department = self.get_object()
        employees = department.employee.all()
        
        stats = {
            'department': department.name,
            'employees_count': employees.count(),
            'active_employees': employees.filter(is_active=True).count(),
            'online_employees': employees.filter(is_online=True).count(),
            'total_clients': Client.objects.filter(
                assigned_employees__department=department
            ).count(),
            'active_clients': Client.objects.filter(
                assigned_employees__department=department,
                status='active'
            ).count(),
        }
        return Response(stats)


class EmployeeViewSet(viewsets.ModelViewSet):
    """
    API ViewSet for Employee management
    
    Endpoints:
    - GET /api/employees/ - List all employees
    - POST /api/employees/ - Create new employee
    - GET /api/employees/{id}/ - Get employee details
    - PUT /api/employees/{id}/ - Update employee
    - DELETE /api/employees/{id}/ - Delete employee
    - GET /api/employees/{id}/clients/ - Get employee's clients
    - GET /api/employees/{id}/stats/ - Get employee statistics
    """
    queryset = Employee.objects.select_related('user', 'department')
    serializer_class = EmployeeSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['department', 'is_active', 'is_online']
    search_fields = ['user__first_name', 'user__last_name']
    ordering_fields = ['last_seen', 'clients_count']
    
    @action(detail=True, methods=['get'])
    def clients(self, request, pk=None):
        """Get all clients assigned to this employee"""
        employee = self.get_object()
        clients = employee.clients.all()
        serializer = ClientSerializer(clients, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """Get employee statistics"""
        from apps.crm.tasks import generate_employee_stats
        employee = self.get_object()
        
        # Get date range from query params
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        
        stats = generate_employee_stats.delay(
            employee_id=str(employee.id),
            start_date=start_date,
            end_date=end_date
        ).get()
        
        return Response(stats)
    
    @action(detail=True, methods=['post'])
    def toggle_online(self, request, pk=None):
        """Toggle employee online status"""
        employee = self.get_object()
        employee.is_online = not employee.is_online
        employee.save(update_fields=['is_online'])
        
        return Response({
            'status': 'success',
            'is_online': employee.is_online
        })



class EmployeeLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API ViewSet for employee Logs (read-only)
    
    Endpoints:
    - GET /api/logs/ - List logs
    - GET /api/logs/{id}/ - Get log details
    """
    queryset = EmployeeLog.objects.select_related('employee', 'client', 'message')
    serializer_class = EmployeeLogSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['employee', 'action', 'client']
    search_fields = ['description']
    ordering_fields = ['timestamp', 'action']
    
    def get_queryset(self):
        """Filter logs for non-admin users to only see their own"""
        user = self.request.user
        if user.is_staff:
            return EmployeeLog.objects.all()
        
        try:
            return EmployeeLog.objects.filter(employee=user.employee)
        except:
            return EmployeeLog.objects.none()



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def stats_view(request):
    return Response({
        "employees_online": Employee.objects.filter(is_online=True).count(),
        "clients_active": Client.objects.filter(status="active").count(),
        "unread_messages": Message.objects.filter(is_read=False).count(),
        "leads": Client.objects.filter(status="lead").count(),
    })