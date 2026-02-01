from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.db.models import Count

from apps.crm.models import (
    Operator, Client, Message, OperatorLog, Department
)
from apps.crm.serializers import (
    OperatorSerializer,
    ClientSerializer,
    MessageSerializer,
    OperatorLogSerializer,
    DepartmentSerializer,
)

from .models import Operator, Client, Message

class DepartmentViewSet(viewsets.ModelViewSet):
    """
    API ViewSet for Department management
    
    Endpoints:
    - GET /api/departments/ - List all departments
    - POST /api/departments/ - Create new department
    - GET /api/departments/{id}/ - Get department details
    - PUT /api/departments/{id}/ - Update department
    - DELETE /api/departments/{id}/ - Delete department
    - GET /api/departments/{id}/operators/ - Get operators in department
    """
    queryset = Department.objects.prefetch_related('operators', 'manager')
    serializer_class = DepartmentSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'created_at']
    
    @action(detail=True, methods=['get'])
    def operators(self, request, pk=None):
        """Get all operators in this department"""
        department = self.get_object()
        operators = department.operators.filter(is_active=True)
        serializer = OperatorSerializer(operators, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """Get department statistics"""
        department = self.get_object()
        operators = department.operators.all()
        
        stats = {
            'department': department.name,
            'operators_count': operators.count(),
            'active_operators': operators.filter(is_active=True).count(),
            'online_operators': operators.filter(is_online=True).count(),
            'total_clients': Client.objects.filter(
                assigned_operator__department=department
            ).count(),
            'active_clients': Client.objects.filter(
                assigned_operator__department=department,
                status='active'
            ).count(),
        }
        return Response(stats)


class OperatorViewSet(viewsets.ModelViewSet):
    """
    API ViewSet for Operator management
    
    Endpoints:
    - GET /api/operators/ - List all operators
    - POST /api/operators/ - Create new operator
    - GET /api/operators/{id}/ - Get operator details
    - PUT /api/operators/{id}/ - Update operator
    - DELETE /api/operators/{id}/ - Delete operator
    - GET /api/operators/{id}/clients/ - Get operator's clients
    - GET /api/operators/{id}/stats/ - Get operator statistics
    """
    queryset = Operator.objects.select_related('user', 'department')
    serializer_class = OperatorSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['department', 'is_active', 'is_online']
    search_fields = ['user__first_name', 'user__last_name', 'telegram_username']
    ordering_fields = ['last_seen', 'clients_count']
    
    @action(detail=True, methods=['get'])
    def clients(self, request, pk=None):
        """Get all clients assigned to this operator"""
        operator = self.get_object()
        clients = operator.clients.all()
        serializer = ClientSerializer(clients, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """Get operator statistics"""
        from apps.crm.tasks import generate_operator_stats
        operator = self.get_object()
        
        # Get date range from query params
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        
        stats = generate_operator_stats.delay(
            operator_id=str(operator.id),
            start_date=start_date,
            end_date=end_date
        ).get()
        
        return Response(stats)
    
    @action(detail=True, methods=['post'])
    def toggle_online(self, request, pk=None):
        """Toggle operator online status"""
        operator = self.get_object()
        operator.is_online = not operator.is_online
        operator.save(update_fields=['is_online'])
        
        return Response({
            'status': 'success',
            'is_online': operator.is_online
        })


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
    - POST /api/clients/{id}/assign_operator/ - Assign operator
    """
    queryset = Client.objects.select_related('assigned_operator')
    serializer_class = ClientSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'assigned_operator']
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
    def assign_operator(self, request, pk=None):
        """Assign operator to client"""
        client = self.get_object()
        operator_id = request.data.get('operator_id')
        
        try:
            from apps.crm.models import Operator
            operator = Operator.objects.get(id=operator_id)
            client.assigned_operator = operator
            client.save(update_fields=['assigned_operator'])
            
            return Response({
                'status': 'success',
                'message': f'Client assigned to {operator}'
            })
        except Operator.DoesNotExist:
            return Response(
                {'error': 'Operator not found'},
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
            OperatorLog.objects.create(
                operator=request.user.operator,
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
    queryset = Message.objects.select_related('operator', 'client')
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['client', 'operator', 'direction', 'message_type']
    ordering_fields = ['created_at']
    
    def perform_create(self, serializer):
        """Save message and log action"""
        message = serializer.save(operator=self.request.user.operator)
        
        # Log action
        OperatorLog.objects.create(
            operator=self.request.user.operator,
            action='message_sent',
            description=f"Message sent to {message.client}",
            client=message.client,
            message=message,
        )


class OperatorLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API ViewSet for Operator Logs (read-only)
    
    Endpoints:
    - GET /api/logs/ - List logs
    - GET /api/logs/{id}/ - Get log details
    """
    queryset = OperatorLog.objects.select_related('operator', 'client', 'message')
    serializer_class = OperatorLogSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['operator', 'action', 'client']
    search_fields = ['description']
    ordering_fields = ['timestamp', 'action']
    
    def get_queryset(self):
        """Filter logs for non-admin users to only see their own"""
        user = self.request.user
        if user.is_staff:
            return OperatorLog.objects.all()
        
        try:
            return OperatorLog.objects.filter(operator=user.operator)
        except:
            return OperatorLog.objects.none()



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def stats_view(request):
    return Response({
        "operators_online": Operator.objects.filter(is_online=True).count(),
        "clients_active": Client.objects.filter(status="active").count(),
        "unread_messages": Message.objects.filter(is_read=False).count(),
        "leads": Client.objects.filter(status="lead").count(),
    })