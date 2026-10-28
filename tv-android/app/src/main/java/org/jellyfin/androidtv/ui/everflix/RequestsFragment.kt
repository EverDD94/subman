package org.jellyfin.androidtv.ui.everflix

import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import android.widget.ImageView
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.fragment.app.Fragment
import androidx.fragment.compose.content
import android.widget.Toast
import kotlinx.coroutines.launch
import org.jellyfin.androidtv.ui.base.Text
import org.jellyfin.androidtv.ui.composable.AsyncImage
import org.jellyfin.androidtv.ui.shared.toolbar.MainRail
import org.jellyfin.androidtv.ui.shared.toolbar.MainToolbarActiveButton
import org.jellyfin.sdk.api.client.ApiClient
import org.koin.compose.koinInject

/** "Mis peticiones": what this person asked for from the TV or the web, and whether it's approved yet. */
class RequestsFragment : Fragment() {
	override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?) = content {
		RequestsScreen()
	}
}

private fun statusLabel(status: String): Pair<String, Color> = when (status) {
	"ya-esta" -> "Ya está para ver" to Color(0xFF2E9E5B)
	"descargando" -> "Descargando" to Color(0xFF3D63E0)
	"aceptada" -> "Aprobada" to Color(0xFF3D63E0)
	"rechazada" -> "No aprobada" to Color(0xFF8A3A3A)
	"no-disponible" -> "Buscándola" to Color(0xFF8A6A1E)
	else -> "Esperando aprobación" to Color(0xFF4A4F5C)
}

private fun downloadLabel(state: String): Pair<String, Color> = when (state) {
	"listo" -> "Ya está para ver" to Color(0xFF2E9E5B)
	"descargando" -> "Descargando" to Color(0xFF3D63E0)
	"buscando", "pidiendo" -> "Buscándola" to Color(0xFF3D63E0)
	"manual" -> "Pulsa en Telegram" to Color(0xFF8A6A1E)
	"no-esta" -> "No la encuentro aún" to Color(0xFF8A6A1E)
	"error" -> "Error" to Color(0xFF8A3A3A)
	else -> "En cola" to Color(0xFF4A4F5C)
}

@Composable
private fun RequestsScreen() {
	if (rememberIsAdmin()) AdminScreen() else MineScreen()
}

/** For the owner: what the others asked for (approve or reject with OK) and the download queue. */
@Composable
private fun AdminScreen() {
	val api = koinInject<ApiClient>()
	val client = remember(api.baseUrl, api.accessToken) { Videotecario(api.baseUrl, api.accessToken) }
	val context = LocalContext.current
	val scope = rememberCoroutineScope()
	var view by remember { mutableStateOf<AdminView?>(null) }
	var failed by remember { mutableStateOf(false) }
	var reload by remember { mutableStateOf(0) }
	LaunchedEffect(client, reload) {
		val v = client.admin()
		failed = v == null
		view = v
	}

	Row(Modifier.fillMaxSize().background(Color(0xFF0B0D12))) {
		MainRail(MainToolbarActiveButton.Requests)
		Column(Modifier.fillMaxSize().padding(start = 24.dp, top = 40.dp, end = 48.dp)) {
			Text("DESCARGAS", color = Color(0xFFF4F2EE), fontSize = 30.sp, fontWeight = FontWeight.W800)
			Text(
				"Primero lo que han pedido los demás (pulsa OK para aprobar o rechazar) y luego lo que se está bajando.",
				color = Color(0xFF9AA0AD),
				fontSize = 14.sp,
				modifier = Modifier.padding(top = 6.dp, bottom = 20.dp),
			)
			val v = view
			val pending = v?.requests.orEmpty().filter { it.status == "pendiente" }
			when {
				failed -> Text("No puedo hablar con Videotecario ahora mismo.", color = Color(0xFF9AA0AD), fontSize = 16.sp)
				v == null -> Text("Cargando…", color = Color(0xFF9AA0AD))
				pending.isEmpty() && v.downloads.isEmpty() -> Text("No hay nada pendiente ni bajándose. Busca algo en Buscar y pulsa «Descargar».", color = Color(0xFF9AA0AD), fontSize = 16.sp)
				else -> LazyVerticalGrid(
					columns = GridCells.Adaptive(130.dp),
					horizontalArrangement = Arrangement.spacedBy(16.dp),
					verticalArrangement = Arrangement.spacedBy(20.dp),
					contentPadding = PaddingValues(bottom = 32.dp),
				) {
					items(pending) { r ->
						StatusCard(r.title, r.year, r.poster, "Pide " + r.who.joinToString(", "), Color(0xFFB8862B), null) {
							android.app.AlertDialog.Builder(context)
								.setTitle(r.title)
								.setItems(arrayOf("Aprobar", "Rechazar", "Ahora no")) { _, which ->
									if (which < 2) scope.launch {
										val error = client.decide(r, accept = which == 0)
										if (error != null) Toast.makeText(context, error, Toast.LENGTH_SHORT).show()
										reload++
									}
								}
								.show()
						}
					}
					items(v.downloads) { d ->
						val (label, color) = downloadLabel(d.state)
						StatusCard(d.title + (d.season?.let { " · T$it" } ?: ""), d.year, d.poster, label, color, null, null)
					}
				}
			}
		}
	}
}

@Composable
private fun MineScreen() {
	val api = koinInject<ApiClient>()
	val client = remember(api.baseUrl, api.accessToken) { Videotecario(api.baseUrl, api.accessToken) }
	var items by remember { mutableStateOf<List<MyRequest>?>(null) }
	LaunchedEffect(client) { items = client.mine() }

	Row(Modifier.fillMaxSize().background(Color(0xFF0B0D12))) {
		MainRail(MainToolbarActiveButton.Requests)
		Column(Modifier.fillMaxSize().padding(start = 24.dp, top = 40.dp, end = 48.dp)) {
			Text("MIS PETICIONES", color = Color(0xFFF4F2EE), fontSize = 30.sp, fontWeight = FontWeight.W800)
			Text(
				"Lo que has pedido. Se descarga cuando se aprueba y aparece solo en tu biblioteca.",
				color = Color(0xFF9AA0AD),
				fontSize = 14.sp,
				modifier = Modifier.padding(top = 6.dp, bottom = 20.dp),
			)
			when {
				items == null -> Text("Cargando…", color = Color(0xFF9AA0AD))
				items!!.isEmpty() -> Text("Aún no has pedido nada. Búscalo en Buscar y pulsa «Pedir».", color = Color(0xFF9AA0AD), fontSize = 16.sp)
				else -> LazyVerticalGrid(
					columns = GridCells.Adaptive(130.dp),
					horizontalArrangement = Arrangement.spacedBy(16.dp),
					verticalArrangement = Arrangement.spacedBy(20.dp),
					contentPadding = PaddingValues(bottom = 32.dp),
				) {
					items(items!!) { RequestCard(it) }
				}
			}
		}
	}
}

@Composable
private fun RequestCard(request: MyRequest) {
	val (label, color) = statusLabel(request.status)
	StatusCard(request.title, request.year, request.poster, label, color, request.why, null)
}

@Composable
private fun StatusCard(title: String, year: Int?, poster: String, label: String, color: Color, why: String?, onClick: (() -> Unit)?) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	val click = if (onClick != null) Modifier.clickable(interactionSource = interaction, indication = null, onClick = onClick) else Modifier.focusable(interactionSource = interaction)
	Column(Modifier.width(130.dp).scale(if (focused) 1.06f else 1f).then(click)) {
		Box(
			Modifier
				.fillMaxWidth()
				.aspectRatio(2f / 3f)
				.clip(RoundedCornerShape(8.dp))
				.then(if (focused) Modifier.border(3.dp, Color(0xFFF4F2EE), RoundedCornerShape(8.dp)) else Modifier)
				.background(Color(0xFF161A22)),
		) {
			if (poster.isNotBlank()) AsyncImage(url = poster, scaleType = ImageView.ScaleType.CENTER_CROP, modifier = Modifier.fillMaxSize())
			Text(
				label,
				color = Color.White,
				fontSize = 11.sp,
				fontWeight = FontWeight.W700,
				modifier = Modifier.align(Alignment.BottomStart).fillMaxWidth().background(color).padding(horizontal = 8.dp, vertical = 4.dp),
			)
		}
		Text(
			title + (year?.let { " ($it)" } ?: ""),
			color = Color(0xFFF4F2EE),
			fontSize = 13.sp,
			fontWeight = FontWeight.W600,
			maxLines = 2,
			overflow = TextOverflow.Ellipsis,
			modifier = Modifier.padding(top = 6.dp),
		)
		if (focused && why != null) Text(why, color = Color(0xFF9AA0AD), fontSize = 11.sp)
	}
}
