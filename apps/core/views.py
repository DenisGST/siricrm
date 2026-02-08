from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from .models import Employee
from .forms import EmployeeForm

def is_admin(user):
    # Временно: админ – это is_superuser; потом можно завязать на Employee.role == admin
    return user.is_superuser

@login_required
@user_passes_test(is_admin)
def employee_list(request):
    employees = Employee.objects.select_related("user", "department")
    return render(request, "core/employees/employee_list.html", {"employees": employees})

@login_required
@user_passes_test(is_admin)
def employee_create(request):
    if request.method == "POST":
        form = EmployeeForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("employee_list")
    else:
        form = EmployeeForm()
    return render(request, "core/employees/employee_form.html", {"form": form})

@login_required
@user_passes_test(is_admin)
def employee_edit(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    if request.method == "POST":
        form = EmployeeForm(request.POST, instance=employee)
        if form.is_valid():
            form.save()
            return redirect("employee_list")
    else:
        form = EmployeeForm(instance=employee)
    return render(request, "core/employees/employee_form.html", {"form": form, "employee": employee})

@login_required
@user_passes_test(is_admin)
def employee_delete(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    if request.method == "POST":
        employee.delete()
        return redirect("employee_list")
    return render(request, "core/employees/employee_confirm_delete.html", {"employee": employee})
