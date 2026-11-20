package x;

import java.util.List;

/**
 * Service interface; security annotations live here, not on the implementation.
 */
public interface GuardedApi {

	/**
	 * Saves a guarded record.
	 */
	@PreAuthorize(
	        "hasAuthority('EDIT_GUARDED')")
	Guarded saveGuarded(Guarded guarded);

	// a line comment between the annotation and the declaration
	@Authorized({ "VIEW_GUARDED",
	        "VIEW_ALL" })
	// comment
	List<Guarded> getGuarded(String name);

	Guarded openGuarded(String id);
}
