from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.db.models import Count

from apps.crm.models import (
    Client, Message
)
from apps.core.models import (
    Employee, EmployeeLog, Department
)
from apps.core.serializers import (
    EmployeeSerializer,
    EmployeeLogSerializer,
    DepartmentSerializer,
)

from apps.crm.serializers import (
    ClientSerializer,
    MessageSerializer,
)

from .models import Employee, Client, Message




class ClientViewSet(viewsets.ModelViewSet):
    """
    API ViewSet for Client management
    
    Endpoints:
    - GET /api/clients/ - List all clients
    - POST /api/clients/ - Create new client
    - GET /api/clients/{id}/ - Get client details
    - PUT /api/clients/{id}/ - Update client
    - DELETE /api/clients/{id}/ - Delete client
    - GET /api/clients/{id}/messages/ - Get client conversation
    - POST /api/clients/{id}/assign_employee/ - Assign employee
    """
    queryset = Client.objects.select_related('assigned_employee')
    serializer_class = ClientSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'assigned_employee']
    search_fields = ['first_name', 'last_name', 'username', 'phone', 'email']
    ordering_fields = ['last_message_at', 'created_at']
    
    @action(detail=True, methods=['get'])
    def messages(self, request, pk=None):
        """Get all messages for this client"""
        client = self.get_object()
        messages = client.messages.all()
        
        # Pagination
        paginate_by = request.query_params.get('limit', 50)
        messages = messages[:int(paginate_by)]
        
        serializer = MessageSerializer(messages, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def assign_employee(self, request, pk=None):
        """Assign employee to client"""
        client = self.get_object()
        employee_id = request.data.get('employee_id')
        
        try:
            from apps.core.models import Employee
            employee = Employee.objects.get(id=employee_id)
            client.assigned_employee = employee
            client.save(update_fields=['assigned_employee'])
            
            return Response({
                'status': 'success',
                'message': f'Client assigned to {employee}'
            })
        except Employee.DoesNotExist:
            return Response(
                {'error': 'Employee not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    
    @action(detail=True, methods=['post'])
    def change_status(self, request, pk=None):
        """Change client status"""
        client = self.get_object()
        new_status = request.data.get('status')
        
        if new_status in dict(Client.STATUS_CHOICES):
            client.status = new_status
            client.save(update_fields=['status'])
            
            # Log action
            EmployeeLog.objects.create(
                employee=request.user.employee,
                action='client_status_changed',
                description=f"Client status changed to {new_status}",
                client=client,
            )
            
            return Response({
                'status': 'success',
                'new_status': new_status
            })
        
        return Response(
            {'error': 'Invalid status'},
            status=status.HTTP_400_BAD_REQUEST
        )


class MessageViewSet(viewsets.ModelViewSet):
    """
    API ViewSet for Message management
    
    Endpoints:
    - GET /api/messages/ - List messages
    - POST /api/messages/ - Send message
    - GET /api/messages/{id}/ - Get message details
    - PUT /api/messages/{id}/ - Update message
    - DELETE /api/messages/{id}/ - Delete message
    """
    queryset = Message.objects.select_related('employee', 'client')
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['client', 'employee', 'direction', 'message_type']
    ordering_fields = ['created_at']
    
    def perform_create(self, serializer):
        """Save message and log action"""
        message = serializer.save(employee=self.request.user.employee)
        
        # Log action
        EmployeeLog.objects.create(
            employee=self.request.user.employee,
            action='message_sent',
            description=f"Message sent to {message.client}",
            client=message.client,
            message=message,
        )

