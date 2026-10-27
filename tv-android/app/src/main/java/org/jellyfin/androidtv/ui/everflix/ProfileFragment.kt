package org.jellyfin.androidtv.ui.everflix

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Bundle
import android.util.Base64
import android.view.LayoutInflater
import android.view.ViewGroup
import android.widget.ImageView
import androidx.activity.compose.LocalActivity
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.fragment.app.Fragment
import androidx.fragment.compose.content
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.jellyfin.androidtv.auth.repository.SessionRepository
import org.jellyfin.androidtv.auth.repository.UserRepository
import org.jellyfin.androidtv.ui.base.Text
import org.jellyfin.androidtv.ui.composable.AsyncImage
import org.jellyfin.androidtv.ui.navigation.ActivityDestinations
import org.jellyfin.androidtv.ui.playback.MediaManager
import org.jellyfin.androidtv.ui.shared.toolbar.MainRail
import org.jellyfin.androidtv.ui.shared.toolbar.MainToolbarActiveButton
import org.jellyfin.androidtv.util.apiclient.getUrl
import org.jellyfin.androidtv.util.apiclient.primaryImage
import org.jellyfin.sdk.api.client.ApiClient
import org.jellyfin.sdk.api.client.extensions.imageApi
import org.jellyfin.sdk.api.client.extensions.itemsApi
import org.jellyfin.sdk.api.client.extensions.personsApi
import org.jellyfin.sdk.api.client.extensions.userApi
import org.jellyfin.sdk.model.api.BaseItemKind
import org.jellyfin.sdk.model.api.ImageType
import org.jellyfin.sdk.model.api.ItemFields
import org.jellyfin.sdk.model.api.ItemSortBy
import org.jellyfin.sdk.model.api.PersonKind
import org.jellyfin.sdk.model.api.SortOrder
import org.koin.compose.koinInject
import timber.log.Timber
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID

private val Bg = Color(0xFF0B0D12)
private val Ink = Color(0xFFF4F2EE)
private val Muted = Color(0xFF9AA0AD)
private val Accent = Color(0xFF3D63E0)

/**
 * "Mi perfil": your photo and name. Jellyfin has no way to pick a photo from the TV, so here you
 * choose a face from what you watch (the cast of your series and movies) and it becomes your
 * Jellyfin profile picture everywhere.
 */
class ProfileFragment : Fragment() {
	override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?) = content {
		ProfileScreen()
	}
}

private data class Face(val id: UUID, val name: String, val role: String?, val tag: String)

@Composable
private fun ProfileScreen() {
	val api = koinInject<ApiClient>()
	val userRepository = koinInject<UserRepository>()
	val sessionRepository = koinInject<SessionRepository>()
	val mediaManager = koinInject<MediaManager>()
	val activity = LocalActivity.current
	val user by userRepository.currentUser.collectAsState()
	val scope = rememberCoroutineScope()
	var faces by remember { mutableStateOf<List<Face>?>(null) }
	var busy by remember { mutableStateOf(false) }
	var message by remember { mutableStateOf<String?>(null) }

	LaunchedEffect(Unit) { faces = loadFaces(api) }

	fun refreshUser() = scope.launch {
		withContext(Dispatchers.IO) { runCatching { api.userApi.getCurrentUser().content } }
			.onSuccess { userRepository.setCurrentUser(it) }
	}

	fun choose(face: Face) {
		val id = user?.id ?: return
		if (busy) return
		busy = true
		message = "Poniendo tu foto…"
		scope.launch {
			val ok = withContext(Dispatchers.IO) { setPhoto(api, id, face) }
			message = if (ok) "¡Listo! Ahora eres ${face.role?.takeIf { it.isNotBlank() } ?: face.name}." else "No se pudo cambiar la foto. Prueba otra vez."
			if (ok) refreshUser()
			busy = false
		}
	}

	fun removePhoto() {
		val id = user?.id ?: return
		if (busy) return
		busy = true
		scope.launch {
			val ok = withContext(Dispatchers.IO) { send(api, "DELETE", listOf("/UserImage?userId=$id", "/Users/$id/Images/Primary"), null) }
			message = if (ok) "Foto quitada." else "No se pudo quitar la foto."
			if (ok) refreshUser()
			busy = false
		}
	}

	Row(Modifier.fillMaxSize().background(Bg)) {
		MainRail(MainToolbarActiveButton.User)
		Column(Modifier.fillMaxSize().padding(start = 24.dp, top = 40.dp, end = 48.dp)) {
			Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(28.dp)) {
				val photo = user?.primaryImage?.getUrl(api, maxHeight = 300)
				Box(
					Modifier.size(120.dp).clip(CircleShape).background(Color(0xFF161A22)).border(3.dp, Accent, CircleShape),
				) {
					if (photo != null) AsyncImage(url = photo, scaleType = ImageView.ScaleType.CENTER_CROP, modifier = Modifier.fillMaxSize())
					else Text(
						user?.name?.take(1)?.uppercase().orEmpty(),
						color = Ink,
						fontSize = 48.sp,
						fontWeight = FontWeight.W800,
						modifier = Modifier.align(Alignment.Center),
					)
				}
				Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
					Text(user?.name.orEmpty(), color = Ink, fontSize = 34.sp, fontWeight = FontWeight.W800)
					Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
						Pill("Cambiar de usuario") {
							mediaManager.clearAudioQueue()
							sessionRepository.destroyCurrentSession()
							activity?.let {
								it.startActivity(ActivityDestinations.startup(it))
								it.finishAfterTransition()
							}
						}
						if (user?.primaryImageTag != null) Pill("Quitar foto") { removePhoto() }
					}
				}
			}

			Text(
				"ELIGE TU FOTO",
				color = Ink,
				fontSize = 20.sp,
				fontWeight = FontWeight.W800,
				modifier = Modifier.padding(top = 32.dp),
			)
			Text(
				message ?: "Caras de lo que ves. La que elijas sale en Everflix y en la web de Jellyfin.",
				color = if (message != null) Ink else Muted,
				fontSize = 14.sp,
				modifier = Modifier.padding(top = 4.dp, bottom = 16.dp),
			)
			when {
				faces == null -> Text("Cargando…", color = Muted)
				faces!!.isEmpty() -> Text("No encontré caras en tu biblioteca.", color = Muted)
				else -> LazyVerticalGrid(
					columns = GridCells.Adaptive(118.dp),
					horizontalArrangement = Arrangement.spacedBy(12.dp),
					verticalArrangement = Arrangement.spacedBy(18.dp),
					contentPadding = PaddingValues(top = 8.dp, bottom = 32.dp),
				) {
					items(faces!!, key = { it.id }) { face ->
						FaceCard(face, api) { choose(face) }
					}
				}
			}
		}
	}
}

@Composable
private fun Pill(label: String, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	Box(
		contentAlignment = Alignment.Center,
		modifier = Modifier
			.height(44.dp)
			.scale(if (focused) 1.06f else 1f)
			.clip(RoundedCornerShape(22.dp))
			.background(if (focused) Ink else Color.White.copy(alpha = 0.14f))
			.clickable(interactionSource = interaction, indication = null, onClick = onClick)
			.padding(horizontal = 22.dp),
	) {
		Text(label, color = if (focused) Color.Black else Ink, fontSize = 15.sp, fontWeight = FontWeight.W700, maxLines = 1)
	}
}

@Composable
private fun FaceCard(face: Face, api: ApiClient, onClick: () -> Unit) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	val url = remember(face) { api.imageApi.getItemImageUrl(itemId = face.id, imageType = ImageType.PRIMARY, tag = face.tag, maxHeight = 240) }
	Column(
		horizontalAlignment = Alignment.CenterHorizontally,
		modifier = Modifier
			.width(118.dp)
			.scale(if (focused) 1.08f else 1f)
			.clickable(interactionSource = interaction, indication = null, onClick = onClick),
	) {
		Box(
			Modifier
				.size(96.dp)
				.clip(CircleShape)
				.background(Color(0xFF161A22))
				.then(if (focused) Modifier.border(3.dp, Ink, CircleShape) else Modifier),
		) {
			AsyncImage(url = url, scaleType = ImageView.ScaleType.CENTER_CROP, modifier = Modifier.fillMaxSize())
		}
		Text(
			face.role?.takeIf { it.isNotBlank() } ?: face.name,
			color = if (focused) Ink else Ink.copy(alpha = 0.8f),
			fontSize = 13.sp,
			fontWeight = FontWeight.W600,
			maxLines = 1,
			overflow = TextOverflow.Ellipsis,
			textAlign = TextAlign.Center,
			modifier = Modifier.padding(top = 8.dp),
		)
		if (!face.role.isNullOrBlank()) Text(
			face.name,
			color = Muted,
			fontSize = 11.sp,
			maxLines = 1,
			overflow = TextOverflow.Ellipsis,
			textAlign = TextAlign.Center,
		)
	}
}

/** The cast of what this person watched lately, then anyone else in the library with a photo. */
private suspend fun loadFaces(api: ApiClient): List<Face> = withContext(Dispatchers.IO) {
	val faces = LinkedHashMap<UUID, Face>()
	runCatching {
		api.itemsApi.getItems(
			includeItemTypes = setOf(BaseItemKind.SERIES, BaseItemKind.MOVIE),
			recursive = true,
			sortBy = setOf(ItemSortBy.DATE_PLAYED, ItemSortBy.DATE_CREATED),
			sortOrder = setOf(SortOrder.DESCENDING),
			fields = setOf(ItemFields.PEOPLE),
			limit = 40,
		).content.items.forEach { item ->
			item.people.orEmpty()
				.filter { it.type == PersonKind.ACTOR }
				.take(8)
				.forEach { p ->
					val tag = p.primaryImageTag ?: return@forEach
					if (p.id !in faces) faces[p.id] = Face(p.id, p.name.orEmpty(), p.role?.substringBefore(" /"), tag)
				}
		}
	}.onFailure { Timber.w(it, "Failed to load cast for profile pictures") }
	if (faces.size < 60) runCatching {
		api.personsApi.getPersons(limit = 200, enableImageTypes = setOf(ImageType.PRIMARY)).content.items.forEach { p ->
			val tag = p.imageTags?.get(ImageType.PRIMARY) ?: return@forEach
			if (p.id !in faces) faces[p.id] = Face(p.id, p.name.orEmpty(), null, tag)
		}
	}.onFailure { Timber.w(it, "Failed to load people for profile pictures") }
	faces.values.take(120)
}

/** Downloads the face, crops it square around the head and uploads it as the user's picture. */
private fun setPhoto(api: ApiClient, userId: UUID, face: Face): Boolean = runCatching {
	val url = api.imageApi.getItemImageUrl(itemId = face.id, imageType = ImageType.PRIMARY, tag = face.tag, maxHeight = 800)
	val conn = URL(url).openConnection() as HttpURLConnection
	api.accessToken?.let { conn.setRequestProperty("X-Emby-Token", it) }
	val bytes = try {
		conn.inputStream.use { it.readBytes() }
	} finally {
		conn.disconnect()
	}
	val source = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: return false
	val side = minOf(source.width, source.height)
	val left = (source.width - side) / 2
	// portraits: the face sits in the upper part of the photo
	val top = ((source.height - side) * 0.18f).toInt()
	val square = Bitmap.createScaledBitmap(Bitmap.createBitmap(source, left, top, side, side), 500, 500, true)
	val jpeg = ByteArrayOutputStream().use {
		square.compress(Bitmap.CompressFormat.JPEG, 90, it)
		it.toByteArray()
	}
	// Jellyfin expects the image base64-encoded in the body
	val body = Base64.encodeToString(jpeg, Base64.NO_WRAP).toByteArray()
	send(api, "POST", listOf("/UserImage?userId=$userId", "/Users/$userId/Images/Primary"), body)
}.onFailure { Timber.w(it, "Failed to set profile picture") }.getOrDefault(false)

/** Newer servers use /UserImage, older ones /Users/{id}/Images/Primary: try them in order. */
private fun send(api: ApiClient, method: String, paths: List<String>, body: ByteArray?): Boolean {
	val base = api.baseUrl?.trimEnd('/') ?: return false
	for (path in paths) {
		val code = runCatching {
			val conn = URL(base + path).openConnection() as HttpURLConnection
			conn.requestMethod = method
			conn.connectTimeout = 6000
			conn.readTimeout = 15000
			api.accessToken?.let { conn.setRequestProperty("X-Emby-Token", it) }
			if (body != null) {
				conn.doOutput = true
				conn.setRequestProperty("Content-Type", "image/jpeg")
				conn.outputStream.use { it.write(body) }
			}
			try {
				conn.responseCode
			} finally {
				conn.disconnect()
			}
		}.getOrDefault(-1)
		if (code in 200..299) return true
		if (code != 404 && code != 405) return false
	}
	return false
}
