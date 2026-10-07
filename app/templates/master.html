<!DOCTYPE html>
<html lang="pt">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Master - Optmiza CRM</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>body { font-family: 'Plus Jakarta Sans', sans-serif; }</style>
</head>
<body class="bg-slate-50 text-slate-800 min-h-screen p-8">
    <div class="max-w-7xl mx-auto">
        <!-- Cabeçalho -->
        <div class="flex justify-between items-center mb-8">
            <div>
                <h1 class="text-3xl font-bold text-slate-900">Painel Master</h1>
                <p class="text-slate-500 text-sm mt-1">Gestão global de empresas e contas B2B</p>
            </div>
            <div class="flex items-center gap-3">
                <!-- Botão que abre o Modal -->
                <button onclick="document.getElementById('modal-nova-empresa').classList.remove('hidden')" class="bg-blue-600 border border-blue-700 text-white px-4 py-2.5 rounded-xl text-sm font-bold hover:bg-blue-700 transition-all shadow-sm flex items-center gap-2">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
                    Nova Empresa
                </button>
                <a href="{{ url_for('main.index') }}" class="bg-white border border-slate-200 text-slate-600 px-4 py-2.5 rounded-xl text-sm font-bold hover:bg-slate-100 hover:text-slate-900 transition-all shadow-sm">
                    Voltar ao CRM
                </a>
            </div>
        </div>

        <!-- Alertas do Sistema -->
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            <div class="mb-6 space-y-2">
            {% for category, message in messages %}
              <div class="p-4 rounded-xl text-sm font-semibold shadow-sm {{ 'bg-green-50 text-green-700 border border-green-200' if category == 'success' else 'bg-red-50 text-red-700 border border-red-200' }}">
                {{ message }}
              </div>
            {% endfor %}
            </div>
          {% endif %}
        {% endwith %}

        <!-- Tabela de Empresas -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
            <div class="overflow-x-auto">
                <table class="w-full text-left border-collapse">
                    <thead>
                        <tr class="bg-slate-50 border-b border-slate-200 text-xs uppercase tracking-widest text-slate-500">
                            <th class="p-5 font-bold">ID</th>
                            <th class="p-5 font-bold">Empresa & Instância</th>
                            <th class="p-5 font-bold text-center">Usuários</th>
                            <th class="p-5 font-bold text-center">Status</th>
                            <th class="p-5 font-bold">Logótipo (PNG)</th>
                            <th class="p-5 font-bold text-right">Ações</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100">
                        {% for empresa in empresas %}
                        <tr class="hover:bg-slate-50/80 transition-colors">
                            <td class="p-5 text-sm font-bold text-slate-400">#{{ empresa.id }}</td>
                            <td class="p-5">
                                <div class="text-sm font-bold text-slate-800">{{ empresa.nome }}</div>
                                <div class="text-[11px] text-slate-400 font-medium mt-1 truncate max-w-[200px]" title="{{ empresa.instancia_whatsapp }}">
                                    {{ empresa.instancia_whatsapp }}
                                </div>
                            </td>
                            <td class="p-5 text-sm text-slate-600 text-center font-bold">
                                {{ total_usuarios.get(empresa.id, 0) }}
                            </td>
                            <td class="p-5 text-center">
                                {% if empresa.is_ativa %}
                                    <span class="inline-flex items-center px-2.5 py-1 rounded-md text-[10px] font-bold uppercase tracking-wider bg-green-100 text-green-700">Ativa</span>
                                {% else %}
                                    <span class="inline-flex items-center px-2.5 py-1 rounded-md text-[10px] font-bold uppercase tracking-wider bg-slate-100 text-slate-500">Inativa</span>
                                {% endif %}
                            </td>
                            <td class="p-5">
                                <div class="flex items-center gap-4">
                                    {% if empresa.logo_b64 %}
                                        <div class="h-12 w-12 flex-shrink-0 bg-white border border-slate-200 rounded-lg flex items-center justify-center overflow-hidden shadow-sm p-1">
                                            <img src="{{ empresa.logo_b64 }}" alt="Logo" class="h-full w-full object-contain">
                                        </div>
                                    {% else %}
                                        <div class="h-12 w-12 flex-shrink-0 bg-slate-50 border border-dashed border-slate-300 rounded-lg flex items-center justify-center text-slate-400 text-[10px] font-semibold">
                                            Vazio
                                        </div>
                                    {% endif %}
                                    
                                    <form action="{{ url_for('main.upload_logo_empresa', id=empresa.id) }}" method="POST" enctype="multipart/form-data" class="flex flex-col gap-1.5">
                                        <input type="file" name="logo" accept=".png" required 
                                               class="w-48 text-[11px] text-slate-500 file:cursor-pointer file:mr-2 file:py-1 file:px-2.5 file:rounded-md file:border-0 file:text-[10px] file:font-bold file:uppercase file:tracking-wider file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100 transition-all cursor-pointer">
                                        <button type="submit" class="self-start bg-slate-800 hover:bg-slate-700 text-white text-[11px] font-bold py-1.5 px-3 rounded-md transition-colors shadow-sm">
                                            Salvar Imagem
                                        </button>
                                    </form>
                                </div>
                            </td>
                            <td class="p-5 text-right">
                                <form action="{{ url_for('main.toggle_empresa', id=empresa.id) }}" method="POST">
                                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                                    {% if empresa.is_ativa %}
                                        <button type="submit" class="text-xs font-bold text-red-600 hover:text-red-700 bg-red-50 hover:bg-red-100 border border-red-100 px-4 py-2 rounded-lg transition-colors">
                                            Suspender
                                        </button>
                                    {% else %}
                                        <button type="submit" class="text-xs font-bold text-green-600 hover:text-green-700 bg-green-50 hover:bg-green-100 border border-green-100 px-4 py-2 rounded-lg transition-colors">
                                            Ativar
                                        </button>
                                    {% endif %}
                                </form>
                            </td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="6" class="p-12 text-center text-slate-500 text-sm font-medium">Nenhuma empresa registada no sistema até ao momento.</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- MODAL: Nova Empresa -->
    <div id="modal-nova-empresa" class="hidden fixed inset-0 z-50 bg-slate-900/40 backdrop-blur-sm flex items-center justify-center p-4">
        <div class="bg-white rounded-2xl shadow-2xl w-full max-w-md overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 flex justify-between items-center bg-slate-50">
                <h3 class="font-bold text-slate-800">Cadastrar Nova Empresa</h3>
                <button onclick="document.getElementById('modal-nova-empresa').classList.add('hidden')" class="text-slate-400 hover:text-slate-600 text-2xl leading-none">&times;</button>
            </div>
            
            <form action="{{ url_for('main.admin_nova_empresa') }}" method="POST" class="p-6 space-y-4">
                <!-- Usamos o mesmo token gerado pela view master -->
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">Nome da Empresa</label>
                    <input type="text" name="nome_empresa" required placeholder="Ex: Optmiza Tecnologia" class="w-full bg-slate-50 border border-slate-200 px-4 py-3 rounded-xl text-sm focus:ring-2 focus:ring-blue-500 outline-none transition-all">
                </div>
                
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">Usuário Administrador</label>
                    <input type="text" name="username" required placeholder="Ex: admin.empresa" class="w-full bg-slate-50 border border-slate-200 px-4 py-3 rounded-xl text-sm focus:ring-2 focus:ring-blue-500 outline-none transition-all">
                </div>
                
                <div>
                    <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">Senha Provisória</label>
                    <input type="password" name="password" required placeholder="••••••••" class="w-full bg-slate-50 border border-slate-200 px-4 py-3 rounded-xl text-sm focus:ring-2 focus:ring-blue-500 outline-none transition-all">
                </div>
                
                <div class="pt-4">
                    <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-3.5 rounded-xl transition-all shadow-[0_4px_14px_0_rgba(37,99,235,0.39)] hover:shadow-[0_6px_20px_rgba(37,99,235,0.23)]">
                        Criar e Ativar Empresa
                    </button>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
